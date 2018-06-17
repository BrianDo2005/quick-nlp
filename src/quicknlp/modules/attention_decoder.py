import warnings

import torch

from quicknlp.utils import assert_dims
from .basic_decoder import Decoder


class AttentionDecoder(Decoder):

    def _train_forward(self, inputs, hidden=None, constraints=None):
        sl, bs = inputs.size()
        emb = self.embedding_layer(inputs)
        final_outputs = []
        for step in emb:
            step = torch.cat([step, self.projection_layer.get_attention_output(step)], dim=-1).unsqueeze_(0)
            step = assert_dims(step, [1, bs, self.emb_size * 2])
            outputs = self._rnn_step(step, hidden=hidden)
            rnn_out = assert_dims(outputs[-1], [1, bs, self.emb_size])
            final_outputs.append(self.projection_layer(rnn_out[0]))
        outputs = torch.cat(final_outputs, dim=0)
        return outputs

    def _beam_forward(self, inputs, hidden, num_beams, constraints=None):
        # ensure keys exist for all beams
        if self.projection_layer.keys is not None and num_beams > 0:
            self.projection_layer.keys = self.projection_layer.keys.repeat(1, num_beams, 1)
        return super()._beam_forward(inputs, hidden=hidden, num_beams=num_beams)

    def _rnn_step(self, output, hidden):
        new_hidden, outputs = [], []
        for layer_index, (rnn, drop) in enumerate(zip(self.decoder_layer.layers, self.decoder_layer.dropouths)):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                output, new_h = rnn(output, hidden[layer_index])
            new_hidden.append(new_h)
            if layer_index != self.nlayers - 1:  # add dropout between every rnn layer but not after last rnn layer
                output = drop(output)
            outputs.append(output)
        self.decoder_layer.hidden = new_hidden
        return outputs
