from collections import defaultdict
from typing import Callable, Optional

import numpy as np
import torch

from sdc.constants import VALID_BASE_METRICS, VALID_AGGREGATORS


def average_displacement_error(ground_truth, predicted):
    r"""Calculates average displacement error
        ADE(y) = (1/T) \sum_{t=1}^T || s_t - s^*_t ||_2
            where T = num_timesteps, y = (s_1, ..., s_T)

    Does not perform any mode aggregation.

    Args:
        ground_truth (np.ndarray): array of shape (n_timestamps, 2)
        predicted (np.ndarray): array of shape (n_modes, n_timestamps, 2)

        Returns:
            np.ndarray: array of shape (n_modes,)
    """
    return np.linalg.norm(ground_truth - predicted, axis=-1).mean(axis=-1)


def final_displacement_error(ground_truth, predicted):
    """Calculates final displacement error
        FDE(y) = (1/T) || s_T - s^*_T ||_2
             where T = num_timesteps, y = (s_1, ..., s_T)

    Does not performs any mode aggregation.

    Args:
        ground_truth (np.ndarray): array of shape (n_timestamps, 2)
        predicted (np.ndarray): array of shape (n_modes, n_timestamps, 2)

    Returns:
        np.ndarray: array of shape (n_modes,)
    """
    return np.linalg.norm(ground_truth - predicted, axis=-1)[:, -1]


def assert_weights_non_negative(weights: np.ndarray):
    if (weights < 0).sum() > 0:
        raise ValueError('Weights are expected to be non-negative.')


def assert_weights_near_one(weights: np.ndarray):
    weights_sum = weights.sum()
    if not np.isclose(weights_sum, 1):
        raise ValueError(
            f'Weights are expected to sum to 1. Sum: {weights_sum}.')


def aggregate_prediction_request_losses(
    aggregator: str,
    per_plan_losses: np.ndarray,
    per_plan_weights: Optional[np.ndarray] = None
) -> np.ndarray:
    """Given ADE or FDE losses for each predicted mode and an aggregator,
    produce a final loss value.

    Args:
        aggregator (str): aggregator type, see below for valid values
        per_plan_losses (np.ndarray): ADE or FDE losses of shape (n_modes,),
            as returned by `average_displacement_error` or
            `final_displacement_error`
        per_plan_weights (np.ndarray): confidence weights of shape (n_modes,)
            associated with each mode

    Returns:
         np.ndarray: scalar loss value
    """
    assert aggregator in {'min', 'avg', 'top1', 'weighted'}

    if aggregator == 'min':
        agg_prediction_loss = np.min(per_plan_losses, axis=-1)
    elif aggregator == 'avg':
        agg_prediction_loss = np.mean(per_plan_losses, axis=-1)
    elif aggregator == 'top1':
        argmax = np.argmax(per_plan_weights)
        agg_prediction_loss = per_plan_losses[argmax]
    elif aggregator == 'weighted':
        # Linear combination of the losses for the generated
        # predictions of a given request, using normalized
        # per-plan confidence weights as coefficients.

        # Assert the weights form a valid distribution.
        assert_weights_near_one(weights=per_plan_weights)
        assert_weights_non_negative(weights=per_plan_weights)

        agg_prediction_loss = np.sum(
            per_plan_weights * per_plan_losses, axis=-1)
    else:
        raise NotImplementedError

    return agg_prediction_loss


def min_ade(ground_truth, predicted):
    return aggregate_prediction_request_losses(
        aggregator='min',
        per_plan_losses=average_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted))


def min_fde(ground_truth, predicted):
    return aggregate_prediction_request_losses(
        aggregator='min',
        per_plan_losses=final_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted))


def avg_ade(ground_truth, predicted):
    return aggregate_prediction_request_losses(
        aggregator='avg',
        per_plan_losses=average_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted))


def avg_fde(ground_truth, predicted):
    return aggregate_prediction_request_losses(
        aggregator='avg',
        per_plan_losses=final_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted))


def top1_ade(ground_truth, predicted, weights):
    return aggregate_prediction_request_losses(
        aggregator='top1',
        per_plan_losses=average_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted),
        per_plan_weights=weights)


def top1_fde(ground_truth, predicted, weights):
    return aggregate_prediction_request_losses(
        aggregator='top1',
        per_plan_losses=final_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted),
        per_plan_weights=weights)


def weighted_ade(ground_truth, predicted, weights, normalize_weights=False):
    if normalize_weights:
        weights = _softmax_normalize(weights)
    return aggregate_prediction_request_losses(
        aggregator='weighted',
        per_plan_losses=average_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted),
        per_plan_weights=weights)


def weighted_fde(ground_truth, predicted, weights, normalize_weights=False):
    if normalize_weights:
        weights = _softmax_normalize(weights)
    return aggregate_prediction_request_losses(
        aggregator='weighted',
        per_plan_losses=final_displacement_error(
            ground_truth=ground_truth,
            predicted=predicted),
        per_plan_weights=weights)


def log_likelihood(ground_truth, predicted, weights, sigma=1.0):
    """Calculates log-likelihood of the ground_truth trajectory
    under the factorized gaussian mixture parametrized by predicted trajectories, weights and sigma.
    Please follow the link below for the metric formulation:
    https://github.com/yandex-research/shifts/blob/195b3214ff41e5b6c197ea7ef3e38552361f29fb/sdc/ysdc_dataset_api/evaluation/log_likelihood_based_metrics.pdf

    Args:
        ground_truth (np.ndarray): ground truth trajectory, (n_timestamps, 2)
        predicted (np.ndarray): predicted trajectories, (n_modes, n_timestamps, 2)
        weights (np.ndarray): confidence weights associated with trajectories, (n_modes,)
        sigma (float, optional): distribution standart deviation. Defaults to 1.0.

    Returns:
        float: calculated log-likelihood
    """
    assert_weights_near_one(weights)
    assert_weights_non_negative(weights)

    displacement_norms_squared = np.sum((ground_truth - predicted) ** 2, axis=-1)
    normalizing_const = np.log(2 * np.pi * sigma ** 2)
    lse_args = np.log(weights) - np.sum(
        normalizing_const + 0.5 * displacement_norms_squared / sigma ** 2, axis=-1)
    max_arg = lse_args.max()
    ll = np.log(np.sum(np.exp(lse_args - max_arg))) + max_arg
    return ll


def corrected_negative_log_likelihood(ground_truth, predicted, weights, sigma=1.0):
    """Calculates corrected negative log-likelihood of the ground_truth trajectory
    under the factorized gaussian mixture parametrized by predicted trajectories, weights and sigma.
    Please follow the link below for the metric formulation:
    https://github.com/yandex-research/shifts/blob/195b3214ff41e5b6c197ea7ef3e38552361f29fb/sdc/ysdc_dataset_api/evaluation/log_likelihood_based_metrics.pdf

    Args:
        ground_truth (np.ndarray): ground truth trajectory, (n_timestamps, 2)
        predicted (np.ndarray): predicted trajectories, (n_modes, n_timestamps, 2)
        weights (np.ndarray): confidence weights associated with trajectories, (n_modes,)

    Returns:
        float: calculated corrected negative log-likelihood
    """
    n_timestamps = ground_truth.shape[0]
    return (
        -log_likelihood(ground_truth, predicted, weights, sigma)
        - n_timestamps * np.log(2 * np.pi * sigma ** 2)
    )


def _softmax_normalize(weights: np.ndarray) -> np.ndarray:
    weights = np.exp(weights - np.max(weights))
    return weights / weights.sum(axis=0)


def batch_mean_metric(
    base_metric: Callable[[np.ndarray, np.ndarray], np.ndarray],
    predictions: np.ndarray,
    ground_truth: np.ndarray,
) -> np.ndarray:
    """During training, we may wish to produce a single prediction
        for each prediction request (i.e., just sample once from the
        posterior predictive; similar to standard training of an MC
        Dropout model). Then, we simply average over the batch dimension.

    Args:
        base_metric: function such as `average_displacement_error`
        predictions: shape (B, T, 2) where B is the number of
            prediction requests in the batch.
        ground_truth: shape (T, 2), there is only one ground truth
            trajectory for each prediction request.
    """
    return np.mean(
        base_metric(predicted=predictions, ground_truth=ground_truth))


"""
Torch loss methods.
Used by baselines in training.
"""


def average_displacement_error_torch(
    ground_truth: torch.Tensor,
    predicted: torch.Tensor,
) -> torch.Tensor:
    r"""Calculates average displacement error
        ADE(y) = (1/T) \sum_{t=1}^T || s_t - s^*_t ||_2
        where T = num_timesteps, y = (s_1, ..., s_T)

    Does not perform any mode aggregation.

    Args:
        ground_truth (torch.Tensor): tensor of shape (n_timestamps, 2)
        predicted (torch.Tensor): tensor of shape (n_modes, n_timestamps, 2)

    Returns:
        torch.Tensor: tensor of shape (n_modes,)
    """
    return torch.mean(torch.norm(predicted - ground_truth, dim=-1), dim=-1)


def final_displacement_error_torch(
    ground_truth: torch.Tensor,
    predicted: torch.Tensor,
) -> torch.Tensor:
    """Computes final displacement error
        FDE(y) = (1/T) || s_T - s^*_T ||_2
        where y = (s_1, ..., s_T)

    Does not perform any mode aggregation.

    Args:
        ground_truth (torch.Tensor): tensor of shape (n_timestamps, 2)
        predicted (torch.Tensor): tensor of shape (n_modes, n_timestamps, 2)

    Returns:
        torch.Tensor: tensor of shape (n_modes,)
    """
    return torch.norm(ground_truth - predicted, dim=-1)[:, -1]


def batch_mean_metric_torch(
    base_metric: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    predictions: torch.Tensor,
    ground_truth: torch.Tensor,
) -> torch.Tensor:
    """During training, we may wish to produce a single prediction
        for each prediction request (i.e., just sample once from the
        posterior predictive; similar to standard training of an MC
        Dropout model). Then, we simply average over the batch dimension.

    For a Torch model we would expect a Torch base metric
        (e.g., `average_displacement_error_torch`), Torch tensor inputs,
        and a torch.Tensor return type for backpropagation.

    Args:
        base_metric: Callable, function such as
            `average_displacement_error_torch`
        predictions: shape (B, T, 2) where B is the number of
            prediction requests in the batch.
        ground_truth: shape (T, 2), there is only one ground truth
            trajectory for each prediction request.
    """
    return torch.mean(
        base_metric(predicted=predictions, ground_truth=ground_truth))


"""
Dataset analysis utilities.
Used to compute aggregate metrics over predictions on a full development dataset.
"""


def compute_all_aggregator_metrics(
    per_plan_confidences: np.ndarray,
    predictions: np.ndarray,
    ground_truth: np.ndarray,
    metric_name: Optional[str] = None
):
    """Batch size B, we assume consistent number of predictions D per scene.

    per_plan_confidences: np.ndarray, shape (B, D), we assume that all
        prediction requests have the same number of proposed plans here.
    predictions: np.ndarray, shape (B, D, T, 2)
    ground_truth: np.ndarray, shape (B, T, 2), there is only one
        ground_truth trajectory for each prediction request.
    metric_name: Optional[str], if specified, compute a particular metric only.
    """
    metrics_dict = defaultdict(list)

    if metric_name is None:
        base_metrics = VALID_BASE_METRICS
    else:
        base_metrics = []
        for metric in VALID_BASE_METRICS:
            if metric.upper() in metric_name:
                base_metrics.append(metric)
        if not base_metrics:
            raise ValueError(f'Invalid metric name {metric_name} specified.')

    if metric_name is None:
        aggregators = VALID_AGGREGATORS
    else:
        aggregators = []
        for agg in VALID_AGGREGATORS:
            if agg in metric_name:
                aggregators.append(agg)
        if not aggregators:
            raise ValueError(f'Invalid metric name {metric_name} specified.')

    for base_metric_name in base_metrics:
        if base_metric_name == 'ade':
            base_metric = average_displacement_error
        elif base_metric_name == 'fde':
            base_metric = final_displacement_error
        else:
            raise NotImplementedError

        # For each prediction request:
        for index, (req_preds, req_gt, req_plan_confs) in enumerate(
                zip(predictions, ground_truth, per_plan_confidences)):
            req_plan_losses = base_metric(
                predicted=req_preds, ground_truth=req_gt)

            for aggregator in aggregators:
                metric_key = f'{aggregator}{base_metric_name.upper()}'
                metrics_dict[metric_key].append(
                    aggregate_prediction_request_losses(
                        aggregator=aggregator,
                        per_plan_losses=req_plan_losses,
                        per_plan_weights=_softmax_normalize(req_plan_confs)))

    metrics_dict = {
        key: np.stack(values) for key, values in metrics_dict.items()}
    return metrics_dict
