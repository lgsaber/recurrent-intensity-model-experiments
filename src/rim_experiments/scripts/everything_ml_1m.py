#!/usr/bin/env python
# coding: utf-8

import pylab as pl
pl.plot()
pl.show()

from rim_experiments import *
from rim_experiments.dataset import *

kw = {
    # "mult": [0, 0.1, 0.2, 0.5, 1, 3, 10, 30, 100],
    "mult": [0, 0.5, 1, 3, 100],
    # "models_to_run": ["Pop", "RNN-Pop"],
}

plot_names = {
    'Rand': ('Rand', '.'),
    'Pop':  ('Pop',  '*'),
    'Hawkes':  ('Hawkes',  '$h$'),
    'HP':  ('Hawkes-Poisson',  '$p$'),
    'BPR': ('BPR', 'x'),
    'RNN': ('RNN', '$1$'),
    'RNN-Pop': ('RNN-Pop', '$2$'),
    'RNN-Hawkes': ('RNN-Hawkes', '$3$'),
    'RNN-HP': ('RNN-HP', '$4$'),
}

D, V = prepare_ml_1m_data()

offline = Experiment(D, V, **kw)
offline.run()

cvx = Experiment(D, V, **kw, cvx=True)
cvx._pretrain_rnn = offline._rnn
cvx.run()

online = Experiment(D, V, **kw, cvx=True, online=True)
online._pretrain_rnn = offline._rnn
online.run()

###### plot item_rec

fig_item_rec, ax = pl.subplots(1, 3, figsize=(7, 2.5), sharex=True, sharey=True)
xname = f'ItemRec Prec@{offline._k1}'
yname = 'item_ppl'
ylabel = 'Item diversity (perplexity)'

hdl = []
for i, (ax, df) in enumerate(zip(ax, [
    offline.get_mtch_(k=offline._k1),
    cvx.get_mtch_(k=cvx._k1),
    online.get_mtch_(k=online._k1),
    ])):
    for name, (label, marker) in plot_names.items():
        if name == 'BPR':
            name = 'BPR-Item'
        hdl.extend(
            ax.plot(df.loc['prec'][name], df.loc[yname][name],
                    label=label, marker=marker, ls=':')
        )
    ax.set_xlabel(xname)
    if i==0:
        ax.set_ylabel(ylabel)
fig_item_rec.legend(
    hdl, [k for k,v in plot_names.values()],
    bbox_to_anchor=(0.1, 0.9, 0.8, 0), loc=3, ncol=4,
    mode="expand", borderaxespad=0.)
fig_item_rec.subplots_adjust(wspace=0.1)
fig_item_rec.show()

###### plot user_rec

fig_user_rec, ax = pl.subplots(1, 3, figsize=(7, 2.5), sharex=True, sharey=True)
xname = f'UserRec Prec@{offline._c1}'
yname = 'user_ppl'
ylabel = 'User diversity (perplexity)'

hdl = []
for i, (ax, df) in enumerate(zip(ax, [
    offline.get_mtch_(c=offline._c1),
    cvx.get_mtch_(c=cvx._c1),
    online.get_mtch_(c=online._c1),
    ])):
    for name, (label, marker) in plot_names.items():
        if name == 'BPR':
            name = 'BPR-User'
        hdl.extend(
            ax.plot(df.loc['prec'][name], df.loc[yname][name],
                    label=label, marker=marker, ls=':')
        )
    ax.set_xlabel(xname)
    if i==0:
        ax.set_ylabel(ylabel)
fig_user_rec.legend(
    hdl, [k for k,v in plot_names.values()],
    bbox_to_anchor=(0.1, 0.9, 0.8, 0), loc=3, ncol=4,
    mode="expand", borderaxespad=0.)
fig_user_rec.subplots_adjust(wspace=0.1)
fig_user_rec.show()
