import torch
from typing import List, Union

import torch.nn as nn

from quicknlp.modules import Decoder, DropoutEmbeddings, Projection, RNNLayers
from quicknlp.modules.hred_encoder import HREDEncoder
from quicknlp.utils import assert_dims, get_kwarg, get_list

HParam = Union[List[int], int]


class HRED(nn.Module):
    """Basic HRED model
    paper: A Hierarchical Latent Variable Encoder-Decoder Model for Generating Dialogues. Iulian Vlad Serban et al. 2016a.
    github: https://github.com/julianser/hed-dlg-truncated
    arxiv: http://arxiv.org/abs/1605.06069
    """

    BPTT_MAX_UTTERANCES = 1000

    def __init__(self, ntoken: int, emb_sz: HParam, nhid: HParam, nlayers: HParam, pad_token: int,
                 eos_token: int, max_tokens: int = 50, share_embedding_layer: bool = False, tie_decoder: bool = True,
                 bidir: bool = False, session_constraint: bool = False, cell_type="gru", **kwargs):
        """

        Args:
            ntoken (int): Number of tokens for the encoder and the decoder
            emb_sz (Union[List[int],int]): Embedding size for the encoder and decoder embeddings
            nhid (Union[List[int],int]): Number of hidden dims for the encoder (first two values) and the decoder
            nlayers (Union[List[int],int]): Number of layers for the encoder and the decoder
            pad_token (int): The  index of the token used for padding
            eos_token (int): The index of the token used for eos
            max_tokens (int): The maximum number of steps the decoder iterates before stopping
            share_embedding_layer (bool): if True the decoder shares its input and output embeddings
            tie_decoder (bool): if True the encoder and the decoder share their embeddings
            bidir (bool): if True use a bidirectional encoder
            session_constraint (bool) If true the session will be concated as a constraint to the decoder input
            **kwargs: Extra embeddings that will be passed to the encoder and the decoder
        """
        super().__init__()
        # allow for the same or different parameters between encoder and decoder
        ntoken, emb_sz, nhid, nlayers = get_list(ntoken), get_list(emb_sz, 2), get_list(nhid, 3), get_list(nlayers, 3)
        dropoutd = get_kwarg(kwargs, name="dropout_d", default_value=0.5)  # output dropout
        dropoute = get_kwarg(kwargs, name="dropout_e", default_value=0.1)  # encoder embedding dropout
        dropoute = get_list(dropoute, 2)
        dropouti = get_kwarg(kwargs, name="dropout_i", default_value=0.65)  # input dropout
        dropouti = get_list(dropouti, 2)
        dropouth = get_kwarg(kwargs, name="dropout_h", default_value=0.3)  # RNN output layers dropout
        dropouth = get_list(dropouth, 3)
        wdrop = get_kwarg(kwargs, name="wdrop", default_value=0.5)  # RNN weights dropout
        wdrop = get_list(wdrop, 3)

        train_init = kwargs.pop("train_init", False)  # Have trainable initial states to the RNNs
        dropoutinit = get_kwarg(kwargs, name="dropout_init", default_value=0.1)  # RNN initial states dropout
        dropoutinit = get_list(dropoutinit, 3)
        self.cell_type = cell_type
        self.nt = ntoken[-1]
        self.pr_force = 1.0
        self.share_embedding_layer = share_embedding_layer
        self.tie_decoder = tie_decoder

        self.encoder = HREDEncoder(
            ntoken=ntoken[0],
            emb_sz=emb_sz[0],
            nhid=nhid[:2],
            nlayers=nlayers[0],
            bidir=bidir,
            cell_type=cell_type,
            dropout_e=dropoute[:2],
            dropout_i=dropouti[:2],
            wdrop=wdrop[:2],
            train_init=train_init,
            dropoutinit=dropoutinit[:2]

        )
        if share_embedding_layer:
            decoder_embedding_layer = self.encoder.embedding_layer
        else:
            decoder_embedding_layer = DropoutEmbeddings(ntokens=ntoken[0],
                                                        emb_size=emb_sz[1],
                                                        dropoute=dropoute[1],
                                                        dropouti=dropouti[1]
                                                        )

        input_size_decoder = kwargs.get("input_size_decoder", emb_sz[1])
        input_size_decoder = input_size_decoder + self.encoder.output_size if session_constraint else input_size_decoder
        decoder_rnn = RNNLayers(input_size=input_size_decoder,
                                output_size=kwargs.get("output_size_decoder", emb_sz[1]),
                                nhid=nhid[2], bidir=False, dropouth=dropouth[2],
                                wdrop=wdrop[2], nlayers=nlayers[2], cell_type=self.cell_type,
                                train_init=train_init,
                                dropoutinit=dropoutinit[2]
                                )
        self.session_constraint = session_constraint
        # allow for changing sizes of decoder output
        input_size = decoder_rnn.output_size
        nhid = emb_sz[1] if input_size != emb_sz[1] else None
        projection_layer = Projection(output_size=ntoken[0], input_size=input_size, nhid=nhid, dropout=dropoutd,
                                      tie_encoder=decoder_embedding_layer if tie_decoder else None
                                      )
        self.decoder = Decoder(
            decoder_layer=decoder_rnn,
            projection_layer=projection_layer,
            embedding_layer=decoder_embedding_layer,
            pad_token=pad_token,
            eos_token=eos_token,
            max_tokens=max_tokens,
        )
        self.decoder_state_linear = nn.Linear(in_features=self.encoder.output_size,
                                              out_features=self.decoder.layers[0].output_size)

    def forward(self, *inputs, num_beams=0):
        with torch.set_grad_enabled(self.training):
            encoder_inputs, decoder_inputs = assert_dims(inputs, [2, None, None])  # dims: [sl, bs] for encoder and decoder
            num_utterances, max_sl, bs = encoder_inputs.size()
            # reset the states for the new batch
            self.reset_encoders(bs)
            outputs, last_output = self.encoder(encoder_inputs)
            state, constraints = self.map_session_hidden_state_to_decoder_init_state(last_output)
            outputs_dec, predictions = self.decoding(decoder_inputs, num_beams, state, constraints=constraints)
        return predictions, [*outputs, *outputs_dec]

    def map_session_hidden_state_to_decoder_init_state(self, last_output):
        state = self.decoder.hidden
        # if there are multiple layers we set the state to the first layer and ignore all others
        # get the session_output of the last layer and the last step
        if self.cell_type == "gru":
            # Tanh seems to deteriorate performance so not used as a nonlinear
            state[0] = self.decoder_state_linear(last_output)  # .tanh()
            constraints = last_output if self.session_constraint else None  # dims  [1, bs, ed]
        else:
            # Tanh seems to deteriorate performance so not used as a nonlinear
            state[0] = self.decoder_state_linear(last_output[0]), self.decoder_state_linear(last_output[1])
            constraints = last_output[0] if self.session_constraint else None  # dims  [1, bs, ed]
        return state, constraints

    def reset_encoders(self, bs):
        self.encoder.reset(bs)
        self.decoder.reset(bs)

    def decoding(self, decoder_inputs, num_beams, state, constraints=None):
        if self.training:
            self.decoder.pr_force = self.pr_force
            nb = 1 if self.pr_force < 1 else 0
        else:
            nb = num_beams
        outputs_dec = self.decoder(decoder_inputs, hidden=state, num_beams=nb, constraints=constraints)
        predictions = outputs_dec[:decoder_inputs.size(0)] if num_beams == 0 else self.decoder.beam_outputs
        return outputs_dec, predictions
