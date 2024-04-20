from typing import Optional, Any
import math

import torch.nn as nn
import torch.nn.functional as F
import math
import numpy as np

from CNNFCN_PatternSeries.models.PSEncoder import PatternSeriesEncoder


def _get_activation_fn(activation):
    if activation == "relu":
        return F.relu
    elif activation == "gelu":
        return F.gelu
    raise ValueError("activation should be relu/gelu, not {}".format(activation))


class PSClassiregressor(nn.Module):
    def __init__(self, feat_dim, max_len, d_model=512,
                 n_heads=8, num_layers=3, d_ff=512, num_classes=100,
                 dropout=0.0, pos_encoding='fixed', activation='gelu', norm='BatchNorm', freeze=False, factor=5):
        super(PSClassiregressor, self).__init__()

        self.max_len = max_len
        self.d_model = d_model
        self.n_heads = n_heads
        self.project_inp = nn.Linear(feat_dim, d_model)

        self.output_layer = nn.Linear(d_model, feat_dim)

        self.act = _get_activation_fn(activation)
        self.dropout = nn.Dropout(dropout)

        self.feat_dim = feat_dim
        self.num_classes = num_classes
        self.output_layer = self.build_output_module(d_model, max_len, num_classes)

        # get latent representation by Reconstruction
        # self.cnn = self.build_recon_module(d_model)

        # Only use CNN
        self.cnn = self.build_cnn_module(dropout, num_classes, max_len)

    def build_output_module(self, d_model, max_len, num_classes):
        output_layer = nn.Linear(d_model * max_len, num_classes)
        # no softmax (or log softmax), because CrossEntropyLoss does this internally. If probabilities are needed,
        # add F.log_softmax and use NLLoss
        return output_layer

    def build_cnn_module(self, dropout, num_classes, max_len):
        # cnn_layer = CNNModel(dropout, num_classes, max_len)
        cnn_layer = PatternSeriesEncoder(max_len, num_classes)
        return cnn_layer

    def forward(self, x_enc, padding_masks):
        inp = x_enc.permute(0, 2, 1)
        output = self.cnn(inp)  # (batch_size, num_classes)

        # 2023/12/29 latent representations for reconstruction
        # g0, latent = self.cnn(layer_out)

        return output, output, output
