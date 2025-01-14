import pandas as pd, numpy as np
import functools

import torch
from torch.utils.data import DataLoader, random_split
from torch.nn.utils.rnn import pack_sequence, pad_packed_sequence

from .word_language_model.model import RNNModel

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from ..util import _LitValidated, empty_cache_on_exit, \
                    ExponentiatedLowRankDataFrame, get_best_gpus


class RNN:
    def __init__(self, item_df,
        num_hidden=128, nlayers=2, max_epochs=5,
        gpus=get_best_gpus() if torch.cuda.is_available() else 0,
        truncated_input_steps=256, truncated_bptt_steps=32):

        self._padded_item_list = [None] + item_df.index.tolist()
        self._truncated_input_steps = truncated_input_steps
        self._collate_fn = functools.partial(_collate_fn,
            tokenize={k:i for i,k in enumerate(self._padded_item_list)},
            truncated_input_steps=truncated_input_steps)

        self.model = _LitRNNModel(RNNModel(
            'GRU', len(self._padded_item_list),
            num_hidden, num_hidden, nlayers, 0, True,
        ), truncated_bptt_steps)

        self.trainer = Trainer(max_epochs=max_epochs, gpus=gpus,
            callbacks=[EarlyStopping(monitor='val_loss')])
        print("trainer log at:", self.trainer.logger.log_dir)

    @functools.lru_cache(2)
    @empty_cache_on_exit
    @torch.no_grad()
    def transform(self, D):
        dataset = D.user_in_test['_hist_items'].values
        collate_fn = functools.partial(self._collate_fn, training=False)
        m, n_events, sample_y = _get_dataset_stats(dataset, collate_fn)
        print(f"transforming {m} users with {n_events} events, "
              f"truncated@{self._truncated_input_steps} per user")
        print(f"sample_y={sample_y}")

        batches = self.trainer.predict(
            dataloaders=DataLoader(dataset, 1000, collate_fn=collate_fn))
        delattr(self.model, "predict_dataloader")

        user_hidden, user_log_bias = [np.concatenate(x) for x in zip(*batches)]
        ind_logits = np.hstack([
            user_hidden, user_log_bias[:, None], np.ones_like(user_log_bias)[:, None]
            ])

        item_hidden = self.model.model.decoder.weight.detach().cpu().numpy()
        item_log_bias = self.model.model.decoder.bias.detach().cpu().numpy()
        col_logits = np.hstack([
            item_hidden, np.ones_like(item_log_bias)[:, None], item_log_bias[:, None]
            ])

        return ExponentiatedLowRankDataFrame(
            ind_logits, col_logits, 1, D.user_in_test.index, self._padded_item_list)


    @empty_cache_on_exit
    def fit(self, D):
        dataset = D.user_df[D.user_df['_hist_len']>0]['_hist_items'].values
        collate_fn = functools.partial(self._collate_fn, training=True)
        m, n_events, sample_y = _get_dataset_stats(dataset, collate_fn)
        print(f"fitting {m} users with {n_events} events, "
              f"truncated@{self._truncated_input_steps} per user")
        print(f"sample_y={sample_y}")

        train_set, valid_set = random_split(dataset, [m*4//5, (m - m*4//5)])
        self.trainer.fit(self.model,
            DataLoader(train_set, 64, collate_fn=collate_fn, shuffle=True),
            DataLoader(valid_set, 64, collate_fn=collate_fn),)
        print("val_loss", self.model.val_loss)

        delattr(self.model, 'train_dataloader')
        delattr(self.model, 'val_dataloader')
        return self


def _collate_fn(batch, tokenize, truncated_input_steps, training):
    if truncated_input_steps>0:
        batch = [seq[-truncated_input_steps:] for seq in batch]
    batch = [[0] + [tokenize[x] for x in seq] for seq in batch]
    batch = [torch.tensor(seq, dtype=torch.int64) for seq in batch]
    batch, lengths = pad_packed_sequence(pack_sequence(batch, False))
    if training:
        return (batch[:-1].T, batch[1:].T) # TBPTT assumes NT layout
    else:
        return (batch, lengths) # RNN default TN layout


def _get_dataset_stats(dataset, collate_fn):
    truncated_input_steps = collate_fn.keywords['truncated_input_steps']
    n_events = sum([min(truncated_input_steps, len(x)) for x in dataset])
    sample = next(iter(DataLoader(dataset, 2, collate_fn=collate_fn, shuffle=True)))
    return len(dataset), n_events, sample[1]


class _LitRNNModel(_LitValidated):
    def __init__(self, model, truncated_bptt_steps):
        super().__init__()
        self.model = model
        self.loss = torch.nn.NLLLoss(ignore_index=0)
        self.truncated_bptt_steps = truncated_bptt_steps

    def forward(self, batch):
        """ output user embedding at lengths-1 positions """
        TN_layout, lengths = batch
        hiddens = self.model.init_hidden(len(lengths))
        TNC_out, _ = self.model.rnn(self.model.encoder(TN_layout), hiddens)
        last_hidden = TNC_out[lengths-1, np.arange(len(lengths))]
        pred_logits = self.model.decoder(last_hidden)
        log_bias = (pred_logits.log_softmax(dim=1) - pred_logits).mean(axis=1)
        return last_hidden.cpu().numpy(), log_bias.cpu().numpy()

    def configure_optimizers(self):
        return torch.optim.Adagrad(self.parameters(), lr=0.1)

    def training_step(self, batch, batch_idx, hiddens=None):
        """ truncated_bptt_steps pass batch[:][:, slice] and hiddens """
        x, y = batch[0].T, batch[1].T   # transpose to TN layout
        if hiddens is None:
            hiddens = self.model.init_hidden(x.shape[1])
        else:
            hiddens = hiddens.detach()
        out, hiddens = self.model(x, hiddens)
        loss = self.loss(out, y.view(-1))
        self.log("train_loss", loss)
        return {'loss': loss, 'hiddens': hiddens}
