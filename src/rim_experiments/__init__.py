import functools, collections, torch, dataclasses, warnings, json
from typing import Dict, List
from rim_experiments.models import *
from rim_experiments.metrics import *
from rim_experiments.dataset import Dataset
from rim_experiments import dataset
from rim_experiments.util import _argsort, cached_property, df_to_coo


@dataclasses.dataclass
class ExperimentResult:
    cvx: bool
    online: bool
    _k1: int
    _c1: int
    _kmax: int
    _cmax: int
    item_ppl: float
    user_ppl: float

    item_rec: Dict[str, Dict[str, float]] = dataclasses.field(default_factory=dict)
    user_rec: Dict[str, Dict[str, float]] = dataclasses.field(default_factory=dict)
    mtch_: Dict[str, List[Dict[str, float]]] = dataclasses.field(default_factory=dict)

    def print_results(self):
        print('\nitem_rec')
        print(pd.DataFrame(self.item_rec).T)
        print('\nuser_rec')
        print(pd.DataFrame(self.user_rec).T)

    def save_results(self, fn):
        with open(fn, 'w') as fp:
            json.dump(dataclasses.asdict(self), fp)

    def get_mtch_(self, k=None, c=None, name="mtch_"):
        y = {}
        for method, x in getattr(self, name).items():
            x = pd.DataFrame(x)
            if k is not None:
                y[method] = x.set_index(['k', 'c']).loc[k].sort_index().T
            else:
                y[method] = x.set_index(['c', 'k']).loc[c].sort_index().T
        return pd.concat(y, axis=1) if len(y) else None


class Experiment:
    """ Produce item_rec / user_rec metrics;
    then sweeps through multipliers for relevance-diversity curve,
    interpreting mult<1 as item min-exposure and mult>=1 as user max-limit
    """
    def __init__(self, D, V,
        mult=[], # [0, 0.1, 0.2, 0.5, 1, 3, 10, 30, 100],
        models_to_run=[
            "Rand", "Pop", "EMA", "Hawkes", "HP",
            "RNN", "RNN-Pop", "RNN-EMA", "RNN-Hawkes", "RNN-HP",
            "BPR-Item", "BPR-User","ALS","LogisticMF"
            ],
        model_hyps={},
        device="cpu",
        cvx=False,
        online=False,
        **mtch_kw
        ):
        self.D = D
        self.V = V

        self.mult = np.array(mult)
        self.models_to_run = models_to_run
        self.model_hyps = model_hyps
        self.device = device

        if online:
            assert cvx, "online requires cvx"
            assert V is not None, "online cvx is trained with explicit valid_mat"

        self.mtch_kw = mtch_kw

        self.results = ExperimentResult(
            cvx, online,
            _k1 = self.D.default_item_rec_top_k,
            _c1 = self.D.default_user_rec_top_c,
            _kmax = len(self.D.item_in_test),
            _cmax = len(self.D.user_in_test),
            item_ppl = self.D.get_stats()['event_df']['item_ppl'],
            user_ppl = self.D.get_stats()['event_df']['user_ppl'],
        )

        # pass-through references
        self.__dict__.update(self.results.__dict__)
        self.print_results = self.results.print_results
        self.get_mtch_ = self.results.get_mtch_


    def metrics_update(self, name, S, T=None):
        target_csr = df_to_coo(self.D.target_df)
        score_mat = self.D.transform(S).values

        if self.online:
            # reindex by valid users and test items to keep dimensions consistent
            valid_mat = self.D.transform(T, self.V.user_in_test.index, 0).values
        elif self.cvx:
            valid_mat = score_mat
        else:
            valid_mat = None

        self.item_rec[name] = evaluate_item_rec(
            target_csr, score_mat, self._k1, device=self.device)
        self.user_rec[name] = evaluate_user_rec(
            target_csr, score_mat, self._c1, device=self.device)

        print(pd.DataFrame({
            'item_rec': self.item_rec[name],
            'user_rec': self.user_rec[name],
            }).T)

        if len(self.mult):
            self.mtch_[name] = self._mtch_update(target_csr, score_mat, valid_mat, name)


    def _mtch_update(self, target_csr, score_mat, valid_mat, name):
        """ assign user/item matches and return evaluation results.
        """
        confs = []
        for m in self.mult:
            if m < 1:
                # lower-bound is interpreted as item min-exposure
                confs.append((self._k1, self._c1 * m, 'lb'))
            else:
                # upper-bound is interpreted as user max-limit
                confs.append((self._k1 * m, self._c1, 'ub'))

        mtch_kw = self.mtch_kw.copy()
        if self.cvx:
            mtch_kw['valid_mat'] = valid_mat
            mtch_kw['prefix'] = f"{name}-{self.online}"
        else:
            mtch_kw['argsort_ij'] = _argsort(score_mat, device=self.device)

        out = []
        for k, c, constraint_type in confs:
            res = evaluate_mtch(
                target_csr, score_mat, k, c, constraint_type=constraint_type,
                cvx=self.cvx, device=self.device, **mtch_kw
            )
            res.update({'k': k, 'c': c})
            out.append(res)

        return out


    def transform(self, model, D):
        if model == "Rand":
            return Rand().transform(D)

        if model == "Pop":
            return Pop().transform(D)

        if model == "EMA":
            return EMA(D.horizon).transform(D) * Pop(0, 1).transform(D)

        if model == "Hawkes":
            return self._hawkes.transform(D) * Pop(0, 1).transform(D)

        if model == "HP":
            return self._hawkes_poisson.transform(D) * Pop(0, 1).transform(D)

        if model == "RNN":
            return self._rnn.transform(D)

        if model == "RNN-Pop":
            return self._rnn.transform(D) * Pop(1, 0).transform(D)

        if model == "RNN-EMA":
            return self._rnn.transform(D) * EMA(D.horizon).transform(D)

        if model == "RNN-Hawkes":
            return self._rnn.transform(D) * self._hawkes.transform(D)

        if model == "RNN-HP":
            return self._rnn.transform(D) * self._hawkes_poisson.transform(D)

        if model == "BPR-Item":
            return LightFM_BPR(item_rec=True).fit(D).transform(D)

        if model == "BPR-User":
            return LightFM_BPR(user_rec=True).fit(D).transform(D)

        if model == "ALS":
            return ALS().fit(D).transform(D)

        if model == "LogisticMF":
            return LogisticMF().fit(D).transform(D)


    def run(self):
        for model in self.models_to_run:
            print("running", model)
            S = self.transform(model, self.D)
            T = self.transform(model, self.V) if self.online else None
            self.metrics_update(model, S, T)


    @cached_property
    def _rnn(self):
        if hasattr(self, '_pretrain_rnn'):
            return self._pretrain_rnn
        fitted = RNN(self.D.item_df, **self.model_hyps.get("RNN", {})).fit(self.D)
        for name, param in fitted.model.named_parameters():
            print(name, param.data.shape)
        return fitted

    @cached_property
    def _hawkes(self):
        return Hawkes(self.D.horizon).fit(self.D)

    @cached_property
    def _hawkes_poisson(self):
        return HawkesPoisson(self._hawkes).fit(self.V)


def main(name, *args, **kw):
    prepare_fn = getattr(dataset, name)
    D, V = prepare_fn(*args)
    self = Experiment(D, V, **kw)
    self.run()
    self.results.print_results()
    return self


def plot_results(self, logy=True):
    """ self is an instance of Experiment or ExperimentResult """
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 2, figsize=(7, 2.5))
    df = [self.get_mtch_(k=self._k1), self.get_mtch_(c=self._c1)]

    xname = [f'ItemRec Prec@{self._k1}', f'UserRec Prec@{self._c1}']
    yname = ['item_ppl', 'user_ppl']

    for ax, df, xname, yname in zip(ax, df, xname, yname):
        if df is not None:
            ax.plot(
                df.loc['prec'].unstack().values.T,
                df.loc[yname].unstack().values.T,
                '+:',
            )
        ax.set_xlabel(xname)
        ax.set_ylabel(yname)
        if logy:
            ax.set_yscale('log')
    fig.legend(
        df.loc['prec'].unstack().index.values,
        bbox_to_anchor=(0.1, 0.9, 0.8, 0), loc=3, ncol=4,
        mode="expand", borderaxespad=0.)
    fig.subplots_adjust(wspace=0.25)
    return fig
