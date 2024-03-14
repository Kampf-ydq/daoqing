import torch
import torch.nn as nn

class TimeSeriesCNNFCN(nn.Module):
    def __init__(self, input_size, num_classes):
        super(TimeSeriesCNNFCN, self).__init__()

        # CNN layers
        self.cnn = nn.Sequential(
            nn.Conv1d(in_channels=1, out_channels=64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),
            nn.Conv1d(in_channels=64, out_channels=128, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2)
        )

        # FCN layers
        self.fcn = nn.Sequential(
            nn.Linear(128 * (input_size // 4), 256),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(256, num_classes),
            nn.Softmax(dim=1)
        )

    def forward(self, x):
        # Reshape input for 1D convolution
        x = x.view(x.size(0), 1, -1)

        # CNN layers
        x = self.cnn(x)

        # Flatten for fully connected layers
        x = x.view(x.size(0), -1)

        # FCN layers
        x = self.fcn(x)

        return x