import math
from itertools import repeat
import collections.abc

import torch
import torch.nn as nn

import hydra

from einops import reduce, rearrange

# Copied from https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/helpers.py
# From PyTorch internals
def _ntuple(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable):
            return x
        return tuple(repeat(x, n))
    return parse


to_1tuple = _ntuple(1)
to_2tuple = _ntuple(2)


# Adapted from https://github.com/huggingface/transformers/blob/master/src/transformers/models/reformer/modeling_reformer.py
class ClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    def __init__(self, d_model, d_inner, num_classes, dropout=0.0, pooling_mode='MEAN',
                 batch_first=False):
        super().__init__()
        assert pooling_mode in ['MEAN', 'SUM', 'CLS'], 'pooling_mode not supported'
        self.pooling_mode = pooling_mode
        self.batch_first = batch_first
        self.dense = nn.Linear(d_model, d_inner)
        self.dropout = nn.Dropout(dropout)
        self.out_proj = nn.Linear(d_inner, num_classes)

    def forward(self, hidden_states, **kwargs):
        """
            hidden_states: (B, S, D) if batch_first else (S, B, D)
        """
        if self.pooling_mode in ['MEAN', 'SUM']:
            hidden_states = reduce(hidden_states,
                                   'b s ... -> b ...' if self.batch_first else 's b ... -> b ...',
                                   self.pooling_mode.lower())
        elif self.pooling_mode == 'CLS':
            hidden_states = hidden_states[:, 0] if self.batch_first else hidden_states[0]
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.dense(hidden_states)
        # Huggingface uses tanh instead of relu
        hidden_states = torch.relu(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.out_proj(hidden_states)
        return hidden_states


def sinusoidal_init_(tensor):
    """
        tensor: (max_len, d_model)
    """
    max_len, d_model = tensor.shape
    position = rearrange(torch.arange(0.0, max_len), 's -> s 1')
    div_term = torch.exp(-math.log(10000.0) * torch.arange(0.0, d_model, 2.0) / d_model)
    tensor[:, 0::2] = torch.sin(position * div_term)
    tensor[:, 1::2] = torch.cos(position * div_term)
    return tensor


# Adapted from https://github.com/pytorch/examples/blob/master/word_language_model/model.py
class PositionalEncoding(nn.Module):
    r"""Inject some information about the relative or absolute position of the tokens
        in the sequence. The positional encodings have the same dimension as
        the embeddings, so that the two can be summed. Here, we use sine and cosine
        functions of different frequencies.
    .. math::
        \text{PosEncoder}(pos, 2i) = sin(pos/10000^(2i/d_model))
        \text{PosEncoder}(pos, 2i+1) = cos(pos/10000^(2i/d_model))
        \text{where pos is the word position and i is the embed idx)
    Args:
        d_model: the embed dim (required).
        dropout: the dropout value (default=0.1).
        max_len: the max. length of the incoming sequence (default=5000).
    Examples:
        >>> pos_encoder = PositionalEncoding(d_model)
    """

    def __init__(self, d_model, dropout=0.1, max_len=5000, batch_first=False, initializer=None):
        super().__init__()
        self.batch_first = batch_first
        self.dropout = nn.Dropout(p=dropout)
        pe = torch.empty(max_len, d_model)
        if initializer is None:
            sinusoidal_init_(pe)
            pe = rearrange(pe, 's d -> 1 s d' if self.batch_first else 's d -> s 1 d')
            self.register_buffer('pe', pe)
        else:
            hydra.utils.call(initializer, pe)
            pe = rearrange(pe, 's d -> 1 s d' if self.batch_first else 's d -> s 1 d')
            self.pe = nn.Parameter(pe)

    def forward(self, x):
        r"""Inputs of forward function
        Args:
            x: the sequence fed to the positional encoder model (required).
        Shape:
            x: [sequence length, batch size, embed dim] if not batch_first else [B, S, D]
            output: [sequence length, batch size, embed dim] if not batch_first else [B, S, D]
        Examples:
            >>> output = pos_encoder(x)
        """
        x = x + (self.pe[:, :x.size(1)] if self.batch_first else self.pe[:x.size(0)])
        return self.dropout(x)


# Adapted from https://github.com/rwightman/pytorch-image-models/blob/master/timm/models/layers/mlp.py
class Mlp(nn.Module):
    """ MLP as used in Vision Transformer, MLP-Mixer and related networks
    """
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU,
                 act_fn=None, drop=0., device=None, dtype=None):
        """TD [2021-10-27] act_fn takes precedence over act_layer if set.
        This is to support Pytorch 1.10 Transformer interface that construct the activation
        *function*, not the activation *layer*.
        """
        factory_kwargs = {'device': device, 'dtype': dtype}
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        drop_probs = to_2tuple(drop)
        self.fc1 = nn.Linear(in_features, hidden_features, **factory_kwargs)
        self.act = act_layer() if act_fn is None else act_fn
        self.drop1 = nn.Dropout(drop_probs[0])
        self.fc2 = nn.Linear(hidden_features, out_features, **factory_kwargs)
        self.drop2 = nn.Dropout(drop_probs[1])

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop1(x)
        x = self.fc2(x)
        x = self.drop2(x)
        return x


class GluMlp(nn.Module):
    """ MLP w/ GLU style gating
    See: https://arxiv.org/abs/1612.08083, https://arxiv.org/abs/2002.05202
    """
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.Sigmoid, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        assert hidden_features % 2 == 0
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features // 2, out_features)
        self.drop = nn.Dropout(drop)

    def init_weights(self):
        # override init of fc1 w/ gate portion set to weight near zero, bias=1
        fc1_mid = self.fc1.bias.shape[0] // 2
        nn.init.ones_(self.fc1.bias[fc1_mid:])
        nn.init.normal_(self.fc1.weight[fc1_mid:], std=1e-6)

    def forward(self, x):
        x = self.fc1(x)
        x, gates = x.chunk(2, dim=-1)
        x = x * self.act(gates)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class GatedMlp(nn.Module):
    """ MLP as used in gMLP
    """
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU,
                 gate_layer=None, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        if gate_layer is not None:
            assert hidden_features % 2 == 0
            self.gate = gate_layer(hidden_features)
            hidden_features = hidden_features // 2  # FIXME base reduction on gate property?
        else:
            self.gate = nn.Identity()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.gate(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class ConvMlp(nn.Module):
    """ MLP using 1x1 convs that keeps spatial dims
    """
    def __init__(
            self, in_features, hidden_features=None, out_features=None, act_layer=nn.ReLU, norm_layer=None, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Conv2d(in_features, hidden_features, kernel_size=1, bias=True)
        self.norm = norm_layer(hidden_features) if norm_layer else nn.Identity()
        self.act = act_layer()
        self.fc2 = nn.Conv2d(hidden_features, out_features, kernel_size=1, bias=True)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.norm(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        return x
