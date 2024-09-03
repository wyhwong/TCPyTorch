from typing import Callable, Optional

import lightning as pl
import matplotlib.pyplot as plt
import numpy as np
import torch
import torchvision
from sklearn.metrics import confusion_matrix, roc_curve
from torch import nn

import pipeline.core.model.utils
from pipeline.schemas import config, constants
from pipeline.utils import logger


local_logger = logger.get_logger(__name__)


class ClassifierModel(pl.LightningModule):
    """Classifier model class"""

    def __init__(
        self,
        model_config: config.ModelConfig,
        denorm_fn: Optional[Callable] = None,
    ) -> None:
        """Initialize the ClassifierModel object

        Args:
            model_config (config.ModelConfig): The model configuration
            denorm_fn (Optional[Callable], optional): The denormalization function. Defaults to None.
        """

        super().__init__()

        local_logger.info("Initializing ClassifierModel with config: %s", model_config)

        self.__model_config = model_config
        self.__denorm_fn = denorm_fn
        self.__classifier = pipeline.core.model.utils.initialize_classifier(
            model_config=self.__model_config,
        )

        self.__loss_fn = nn.CrossEntropyLoss()
        self.__optimizers: Optional[list[torch.optim.Optimizer]] = None
        self.__schedulers: Optional[list[torch.optim.lr_scheduler.LRScheduler]] = None

        self.__y_test_true: torch.Tensor = torch.tensor([])
        self.__y_test_pred: torch.Tensor = torch.tensor([])

    def training_setup(
        self,
        num_epochs: int,
        optimizer_config: config.OptimizerConfig,
        scheduler_config: config.SchedulerConfig,
        input_sample: Optional[torch.Tensor] = None,
    ) -> None:
        """Setup the training configuration

        Args:
            num_epochs (int): The number of epochs
            optimizer_config (config.OptimizerConfig): The optimizer configuration
            scheduler_config (config.SchedulerConfig): The scheduler configuration
            input_sample (Optional[torch.Tensor], optional): The input sample for onnx export,
        """

        local_logger.info(
            "Setting up training with num_epochs: %d, optimizer_config: %s, scheduler_config: %s",
            num_epochs,
            optimizer_config,
            scheduler_config,
        )

        self.__optimizers = [
            pipeline.core.model.utils.initialize_optimizer(
                params=self.__classifier.parameters(),
                optimizer_config=optimizer_config,
            )
        ]
        self.__schedulers = [
            pipeline.core.model.utils.initialize_scheduler(
                optimizer=optim,
                scheduler_config=scheduler_config,
                num_epochs=num_epochs,
            )
            for optim in self.__optimizers
        ]

        if input_sample is not None:
            self.example_input_array = input_sample

    def configure_optimizers(self):
        """Configure the optimizers and schedulers."""

        return tuple(
            [
                {
                    "optimizer": optim,
                    "lr_scheduler": {"scheduler": sched, "interval": "step", "frequency": 1},
                }
                for optim, sched in zip(self.__optimizers, self.__schedulers)
            ]
        )

    def __common_step(
        self,
        batch: tuple[torch.Tensor, torch.Tensor],
        batch_idx: int,  # pylint: disable=unused-argument
        phase: constants.Phase,
    ) -> torch.Tensor:
        """Common step for training, validation, and test

        Args:
            batch (tuple[torch.Tensor, torch.Tensor]): The input batch
            batch_idx (int): The batch index
            phase (constants.Phase): The phase

        Returns:
            torch.Tensor: The loss
        """

        x, y = batch

        if batch_idx == 0:
            x_denorm = self.__denorm_fn(x) if self.__denorm_fn else x
            grid = torchvision.utils.make_grid(x_denorm)
            # Here we ignore the type, expected message:
            # "Attribute 'experiment' is not defined for 'Optional[LightningLoggerBase]'"
            self.logger.experiment.add_image(  # type: ignore
                f"sample_images_{phase}",
                grid,
                self.current_epoch,
            )

        logits = self.forward(x)
        loss = self.__loss_fn(logits, y)
        self.log(name=phase(constants.Criterion.LOSS), value=loss, on_step=True)

        acc = (logits.argmax(dim=1) == y).float().mean()
        self.log(name=phase(constants.Criterion.ACCURACY), value=acc, on_step=True)

        return loss

    def training_step(
        self,
        batch: tuple[torch.Tensor, torch.Tensor],
        batch_idx: int,
    ) -> torch.Tensor:
        """Training step

        Args:
            batch (tuple[torch.Tensor, torch.Tensor]): The input batch
            batch_idx (int): The batch index

        Returns:
            torch.Tensor: The loss
        """

        loss = self.__common_step(
            batch=batch,
            batch_idx=batch_idx,
            phase=constants.Phase.TRAINING,
        )
        return loss

    def validation_step(
        self,
        batch: tuple[torch.Tensor, torch.Tensor],
        batch_idx: int,
    ) -> torch.Tensor:
        """Validation step

        Args:
            batch (tuple[torch.Tensor, torch.Tensor]): The input batch
            batch_idx (int): The batch index

        Returns:
            torch.Tensor: The loss
        """

        loss = self.__common_step(
            batch=batch,
            batch_idx=batch_idx,
            phase=constants.Phase.VALIDATION,
        )
        return loss

    def test_step(
        self,
        batch: tuple[torch.Tensor, torch.Tensor],
        batch_idx: int,  # pylint: disable=unused-argument
    ) -> torch.Tensor:
        """Test step

        Args:
            batch (tuple[torch.Tensor, torch.Tensor]): The input batch
            batch_idx (int): The batch index

        Returns:
            torch.Tensor: The loss
        """

        x, y = batch
        logits = self.forward(x)
        loss = self.__loss_fn(logits, y)
        self.log(name=constants.Phase.TESTING(constants.Criterion.LOSS), value=loss, on_step=True)
        acc = (logits.argmax(dim=1) == y).float().mean()
        self.log(name=constants.Phase.TESTING(constants.Criterion.ACCURACY), value=acc, on_step=True)

        probis = nn.functional.softmax(logits, dim=1)
        self.__y_test_true = torch.cat([self.__y_test_true.to(y.device), y])
        self.__y_test_pred = torch.cat([self.__y_test_pred.to(logits.device), probis])
        return loss

    def on_test_epoch_end(self) -> None:
        """Test epoch end"""

        for i in range(self.__y_test_pred.shape[1]):  # Iterate over each class
            fpr, tpr, _ = roc_curve(
                y_true=(self.__y_test_true == i).cpu().numpy(),
                y_score=self.__y_test_pred[:, i].cpu().numpy(),
            )
            self.plot_roc_curve(fpr, tpr, i)

        y_true = self.__y_test_true.cpu().numpy()
        y_pred = self.__y_test_pred.argmax(dim=1).cpu().numpy()
        self.plot_confusion_matrix(y_true, y_pred)

        self.__y_test_true = torch.tensor([])
        self.__y_test_pred = torch.tensor([])

    def plot_roc_curve(self, fpr: np.ndarray, tpr: np.ndarray, class_index: int) -> None:
        """Plot the ROC curve

        Args:
            fpr (np.ndarray): The false positive rate
            tpr (np.ndarray): The true positive rate
            class_index (int): The class index
        """

        plt.figure(figsize=(10, 10))
        plt.plot(fpr, tpr, label=f"{self.__model_config.backbone} (AUC = {np.trapz(tpr, fpr):.2f})")
        plt.plot([0, 1], [0, 1], color="navy", linestyle="--", label="Random")
        plt.xlabel("False Positive Rate")
        plt.ylabel("True Positive Rate")
        plt.title(f"ROC Curve for Class {class_index}")
        plt.legend(loc="lower right")

        self.logger.experiment.add_figure(  # type: ignore
            f"ROC Curve Class {class_index} ({self.__model_config.backbone})",
            plt.gcf(),
            self.current_epoch,
        )
        plt.close()

    def plot_confusion_matrix(self, y_true: np.ndarray, y_pred: np.ndarray) -> None:
        """Plot the confusion matrix

        Args:
            y_true (np.ndarray): The true labels
            y_pred (np.ndarray): The predicted labels
        """

        cm = confusion_matrix(y_true, y_pred)
        plt.figure(figsize=(10, 10))
        plt.imshow(cm, interpolation="nearest", cmap=plt.cm.Blues)  # type: ignore
        plt.title(f"Confusion Matrix ({self.__model_config.backbone})")
        plt.colorbar()
        tick_marks = np.arange(len(np.unique(y_true)))
        plt.yticks(tick_marks, tick_marks)  # type: ignore
        plt.ylabel("True Label")
        plt.xticks(tick_marks, tick_marks)  # type: ignore
        plt.xlabel("Predicted Label")

        # Add text annotations
        thresh = cm.max() / 2.0
        for i, j in np.ndindex(cm.shape):
            plt.text(
                j,
                i,
                format(cm[i, j], "d"),
                horizontalalignment="center",
                color="white" if cm[i, j] > thresh else "black",
            )

        # Textual log
        local_logger.info("Confusion Matrix:\n%s", cm)

        # Log to TensorBoard
        self.logger.experiment.add_figure(  # type: ignore
            "Confusion Matrix",
            plt.gcf(),
            self.current_epoch,
        )
        plt.close()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass

        Args:
            x (torch.Tensor): The input tensor

        Returns:
            torch.Tensor: The output tensor
        """

        return self.__classifier(x.to(self.dtype))
