import torch
import torch.nn.functional as F
import pytorch_lightning as pl
import torchmetrics

# class Embedding: # use this for huggingface compatibiltiy?
#      self.encoder = ...
#      self.adapter = ...
#     def forward(self, x)
#         x = self.encoder(x)
#         x = self.adapter(x)
#         return x (edited)
# 11:14
# class BiEncoder(SentenceTransformer): # this one is for training
#     self.encoder = Embedding()
#    def forward(self, x, y):
#         x = self.encoder(x)
#         y = self.encoder(y)
#         return cosine(x, y) (edited)


# Similarity Model
class SimilarityModel(pl.LightningModule):
    def __init__(self, embedding_size, n_dims, dropout_fraction, lr, use_relu):
        super(SimilarityModel, self).__init__()
        self.matrix = torch.nn.Parameter(
            torch.rand(
                embedding_size,
                n_dims,
            )
        )
        torch.nn.init.xavier_uniform_(self.matrix)
        self.dropout_fraction = dropout_fraction
        self.lr = lr
        self.use_relu = use_relu

        self.recall = torchmetrics.Recall(task="binary")
        self.f1 = torchmetrics.F1Score(num_classes=2, task="binary")
        self.precision = torchmetrics.Precision(task="binary")
        self.acc = torchmetrics.Accuracy(task="binary")
        self.auc = torchmetrics.AUROC(task="binary")
        self.save_hyperparameters()

    def forward(self, embedding_1, embedding_2):
        # modify to call encode
        # maybe make this encode
        e1 = F.dropout(embedding_1, p=self.dropout_fraction)
        e2 = F.dropout(embedding_2, p=self.dropout_fraction)
        matrix = self.matrix if not self.use_relu else F.relu(self.matrix)
        modified_embedding_1 = e1 @ matrix
        modified_embedding_2 = e2 @ matrix
        similarity = F.cosine_similarity(modified_embedding_1, modified_embedding_2)
        return similarity.unsqueeze(-1)  # Adding a dimension to match target shape

    def encode(self, embedding):
        # user can call this from modal endpoint, model after endpoint
        # look into how huggingface inference works, make it compatible
        # returns an actual embedding
        e = F.dropout(embedding, p=self.dropout_fraction)
        matrix = self.matrix if not self.use_relu else F.relu(self.matrix)
        modified_embedding = e @ matrix
        return modified_embedding

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.lr)

    def training_step(self, batch, batch_idx):
        embedding_1, embedding_2 = batch
        similarity = self(embedding_1, embedding_2)
        target_similarity = torch.ones(similarity.shape, device=self.device)  # not sure
        pos_weight = torch.tensor([0.89 / 0.11], device=self.device)
        loss = F.binary_cross_entropy_with_logits(
            similarity, target_similarity, pos_weight=pos_weight, reduction="mean"
        )
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        embedding_1, embedding_2 = batch
        similarity = self(embedding_1, embedding_2)
        target_similarity = torch.ones(similarity.shape, device=self.device)  # not sure
        pos_weight = torch.tensor([0.89 / 0.11], device=self.device)
        loss = F.binary_cross_entropy_with_logits(
            similarity, target_similarity, pos_weight=pos_weight, reduction="mean"
        )
        self.log("val_loss", loss)
        pred_labels = torch.sigmoid(similarity) > 0.5

        self.log("val_recall", self.recall(pred_labels.int(), target_similarity.int()))
        self.log("val_f1", self.f1(pred_labels.int(), target_similarity.int()))
        self.log("val_acc", self.acc(pred_labels.int(), target_similarity.int()))
        self.log(
            "val_precision", self.precision(pred_labels.int(), target_similarity.int())
        )
        self.log(
            "val_auc",
            self.auc(
                torch.sigmoid(similarity).float().squeeze(-1),
                target_similarity.float().squeeze(-1),
            ),
        )
        self.log("val_removed", 1 - (sum(pred_labels) / len(pred_labels)))

    def test_step(self, batch, batch_idx):
        embedding_1, embedding_2 = batch
        similarity = self(embedding_1, embedding_2)
        target_similarity = torch.ones(similarity.shape, device=self.device)  # not sure
        pos_weight = torch.tensor([0.89 / 0.11], device=self.device)
        loss = F.binary_cross_entropy_with_logits(
            similarity, target_similarity, pos_weight=pos_weight, reduction="mean"
        )
        self.log("test_loss", loss)
        pred_labels = torch.sigmoid(similarity) > 0.5
        self.log("test_recall", self.recall(pred_labels.int(), target_similarity.int()))
        self.log("test_f1", self.f1(pred_labels.int(), target_similarity.int()))
        self.log("test_acc", self.acc(pred_labels.int(), target_similarity.int()))
        self.log(
            "test_precision", self.precision(pred_labels.int(), target_similarity.int())
        )
        self.log(
            "test_auc",
            self.auc(
                torch.sigmoid(similarity).float().squeeze(-1),
                target_similarity.float().squeeze(-1),
            ),
        )
