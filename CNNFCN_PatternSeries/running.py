import json
import logging
import os
import pickle
import sys
import time
import traceback
from collections import OrderedDict
from datetime import datetime

import numpy as np
import sklearn
import torch
from torch import nn
from torch.utils.data import DataLoader

from data_loader.dataset import ClassiregressionDataset, collate_superv
from models.loss import l2_reg_loss
from utils import utils, analysis, openmax

logger = logging.getLogger('__main__')

NEG_METRICS = {'loss'}  # metrics for which "better" is less

val_times = {"total_time": 0, "count": 0}


def pipeline_factory(config):
    """For the task specified in the configuration returns the corresponding combination of
    Dataset class, collate function and Runner class."""

    task = config['task']
    if (task == "classification") or (task == "regression"):
        return ClassiregressionDataset, collate_superv, SupervisedRunner
    else:
        raise NotImplementedError("Task '{}' not implemented".format(task))


def setup(args):
    """Prepare training session: read configuration from file (takes precedence), create directories.
    Input:
        args: arguments object from argparse
    Returns:
        config: configuration dictionary
    """

    config = args.__dict__  # configuration dictionary

    if args.config_filepath is not None:
        logger.info("Reading configuration ...")
        try:  # dictionary containing the entire configuration settings in a hierarchical fashion
            config.update(utils.load_config(args.config_filepath))
        except:
            logger.critical("Failed to load configuration file. Check JSON syntax and verify that files exist")
            traceback.print_exc()
            sys.exit(1)

    # Create output directory
    initial_timestamp = datetime.now()
    output_dir = config['output_dir']
    if not os.path.isdir(output_dir):
        raise IOError(
            "Root directory '{}', where the directory of the experiment will be created, must exist".format(output_dir))

    output_dir = os.path.join(output_dir, config['experiment_name'])

    formatted_timestamp = initial_timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    config['initial_timestamp'] = formatted_timestamp
    '''if (not config['no_timestamp']) or (len(config['experiment_name']) == 0):
        rand_suffix = "".join(random.choices(string.ascii_letters + string.digits, k=3))
        output_dir += "_" + formatted_timestamp + "_" + rand_suffix'''
    output_dir += "_" + str(config["masking_ratio"]) + "_" + str(config["mean_mask_length"])
    config['output_dir'] = output_dir
    config['save_dir'] = os.path.join(output_dir, 'checkpoints')
    config['pred_dir'] = os.path.join(output_dir, 'predictions')
    config['tensorboard_dir'] = os.path.join(output_dir, 'tb_summaries')
    utils.create_dirs([config['save_dir'], config['pred_dir'], config['tensorboard_dir']])

    # Save configuration as a (pretty) json file
    with open(os.path.join(output_dir, 'configuration.json'), 'w') as fp:
        json.dump(config, fp, indent=4, sort_keys=True)

    logger.info("Stored configuration file in '{}'".format(output_dir))

    return config


def fold_evaluate(dataset, model, device, loss_module, target_feats, config, dataset_name):
    allfolds = {'target_feats': target_feats,
                # list of len(num_folds), each element: list of target feature integer indices
                'predictions': [],
                # list of len(num_folds), each element: (num_samples, seq_len, feat_dim) prediction per sample
                'targets': [],
                # list of len(num_folds), each element: (num_samples, seq_len, feat_dim) target/original input per sample
                'target_masks': [],
                # list of len(num_folds), each element: (num_samples, seq_len, feat_dim) boolean mask per sample
                'metrics': [],  # list of len(num_folds), each element: (num_samples, num_metrics) metric per sample
                'IDs': []}  # list of len(num_folds), each element: (num_samples,) ID per sample

    for i, tgt_feats in enumerate(target_feats):

        dataset.mask_feats = tgt_feats  # set the transduction target features

        loader = DataLoader(dataset=dataset,
                            batch_size=config['batch_size'],
                            shuffle=False,
                            num_workers=config['num_workers'],
                            pin_memory=True,
                            collate_fn=lambda x: collate_unsuperv(x, max_len=config['max_seq_len']))

        evaluator = UnsupervisedRunner(model, loader, device, loss_module,
                                       print_interval=config['print_interval'], console=config['console'])

        logger.info("Evaluating {} set, fold: {}, target features: {}".format(dataset_name, i, tgt_feats))
        aggr_metrics, per_batch = evaluate(evaluator)

        metrics_array = convert_metrics_per_batch_to_per_sample(per_batch['metrics'], per_batch['target_masks'])
        metrics_array = np.concatenate(metrics_array, axis=0)
        allfolds['metrics'].append(metrics_array)
        allfolds['predictions'].append(np.concatenate(per_batch['predictions'], axis=0))
        allfolds['targets'].append(np.concatenate(per_batch['targets'], axis=0))
        allfolds['target_masks'].append(np.concatenate(per_batch['target_masks'], axis=0))
        allfolds['IDs'].append(np.concatenate(per_batch['IDs'], axis=0))

        metrics_mean = np.mean(metrics_array, axis=0)
        metrics_std = np.std(metrics_array, axis=0)
        for m, metric_name in enumerate(list(aggr_metrics.items())[1:]):
            logger.info("{}:: Mean: {:.3f}, std: {:.3f}".format(metric_name, metrics_mean[m], metrics_std[m]))

    pred_filepath = os.path.join(config['pred_dir'], dataset_name + '_fold_transduction_predictions.pickle')
    logger.info("Serializing predictions into {} ... ".format(pred_filepath))
    with open(pred_filepath, 'wb') as f:
        pickle.dump(allfolds, f, pickle.HIGHEST_PROTOCOL)


def convert_metrics_per_batch_to_per_sample(metrics, target_masks):
    """
    Args:
        metrics: list of len(num_batches), each element: list of len(num_metrics), each element: (num_active_in_batch,) metric per element
        target_masks: list of len(num_batches), each element: (batch_size, seq_len, feat_dim) boolean mask: 1s active, 0s ignore
    Returns:
        metrics_array = list of len(num_batches), each element: (batch_size, num_metrics) metric per sample
    """
    metrics_array = []
    for b, batch_target_masks in enumerate(target_masks):
        num_active_per_sample = np.sum(batch_target_masks, axis=(1, 2))
        batch_metrics = np.stack(metrics[b], axis=1)  # (num_active_in_batch, num_metrics)
        ind = 0
        metrics_per_sample = np.zeros((len(num_active_per_sample), batch_metrics.shape[1]))  # (batch_size, num_metrics)
        for n, num_active in enumerate(num_active_per_sample):
            new_ind = ind + num_active
            metrics_per_sample[n, :] = np.sum(batch_metrics[ind:new_ind, :], axis=0)
            ind = new_ind
        metrics_array.append(metrics_per_sample)
    return metrics_array


def evaluate(evaluator):
    """Perform a single, one-off evaluation on an evaluator object (initialized with a dataset)"""

    eval_start_time = time.time()
    with torch.no_grad():
        aggr_metrics, per_batch = evaluator.evaluate(epoch_num=None, keep_all=True)
    eval_runtime = time.time() - eval_start_time
    print()
    print_str = 'Evaluation Summary: '
    for k, v in aggr_metrics.items():
        if v is not None:
            print_str += '{}: {:8f} | '.format(k, v)
    logger.info(print_str)
    logger.info("Evaluation runtime: {} hours, {} minutes, {} seconds\n".format(*utils.readable_time(eval_runtime)))

    return aggr_metrics, per_batch


def validate(val_evaluator, tensorboard_writer, config, best_metrics, best_value, epoch):
    """Run an evaluation on the validation set while logging metrics, and handle outcome"""

    logger.info("Evaluating on validation set ...")
    eval_start_time = time.time()
    with torch.no_grad():
        aggr_metrics, per_batch = val_evaluator.evaluate(epoch, keep_all=True)
    eval_runtime = time.time() - eval_start_time
    logger.info("Validation runtime: {} hours, {} minutes, {} seconds\n".format(*utils.readable_time(eval_runtime)))

    global val_times
    val_times["total_time"] += eval_runtime
    val_times["count"] += 1
    avg_val_time = val_times["total_time"] / val_times["count"]
    avg_val_batch_time = avg_val_time / len(val_evaluator.dataloader)
    avg_val_sample_time = avg_val_time / len(val_evaluator.dataloader.dataset)
    logger.info("Avg val. time: {} hours, {} minutes, {} seconds".format(*utils.readable_time(avg_val_time)))
    logger.info("Avg batch val. time: {} seconds".format(avg_val_batch_time))
    logger.info("Avg sample val. time: {} seconds".format(avg_val_sample_time))

    print()
    print_str = 'Epoch {} Validation Summary: '.format(epoch)
    for k, v in aggr_metrics.items():
        tensorboard_writer.add_scalar('{}/val'.format(k), v, epoch)
        print_str += '{}: {:8f} | '.format(k, v)
    logger.info(print_str)

    if config['key_metric'] in NEG_METRICS:
        condition = (aggr_metrics[config['key_metric']] < best_value)
    else:
        condition = (aggr_metrics[config['key_metric']] > best_value)
    if condition:
        best_value = aggr_metrics[config['key_metric']]
        utils.save_model(os.path.join(config['save_dir'], 'model_best.pth'), epoch, val_evaluator.model)
        best_metrics = aggr_metrics.copy()

        pred_filepath = os.path.join(config['pred_dir'], 'best_predictions')

        """
        Solve ValueError: setting an array element with a sequence. The requested array has an inhomogeneous shape
        20231002
        """
        for key, val in per_batch.items():
            per_batch[key] = np.asarray(val, dtype=object)
        np.savez(pred_filepath, **per_batch)

    return aggr_metrics, best_metrics, best_value, per_batch


def check_progress(epoch):
    if epoch in [100, 140, 160, 220, 280, 340]:
        return True
    else:
        return False


class BaseRunner(object):

    def __init__(self, model, dataloader, device, loss_module, optimizer=None, l2_reg=None, print_interval=10,
                 console=True):
        self.model = model
        self.dataloader = dataloader
        self.device = device
        self.optimizer = optimizer
        self.loss_module = loss_module
        self.l2_reg = l2_reg
        self.print_interval = print_interval
        self.printer = utils.Printer(console=console)

        self.epoch_metrics = OrderedDict()

    def train_epoch(self, epoch_num=None):
        raise NotImplementedError('Please override in child class')

    def evaluate(self, epoch_num=None, keep_all=True):
        raise NotImplementedError('Please override in child class')

    def print_callback(self, i_batch, metrics, prefix=''):
        total_batches = len(self.dataloader)

        template = "{:5.1f}% | batch: {:9d} of {:9d}"
        content = [100 * (i_batch / total_batches), i_batch, total_batches]
        for met_name, met_value in metrics.items():
            template += "\t|\t{}".format(met_name) + ": {:g}"
            content.append(met_value)

        dyn_string = template.format(*content)
        dyn_string = prefix + dyn_string
        self.printer.print(dyn_string)


class UnsupervisedRunner(BaseRunner):

    def train_epoch(self, epoch_num=None):

        self.model = self.model.train()

        epoch_loss = 0  # total loss of epoch
        total_active_elements = 0  # total unmasked elements in epoch
        for i, batch in enumerate(self.dataloader):

            X, targets, target_masks, padding_masks, IDs = batch

            targets = targets.to(self.device)
            target_masks = target_masks.to(self.device)  # 1s: mask and predict, 0s: unaffected input (ignore)
            padding_masks = padding_masks.to(self.device)  # 0s: ignore

            predictions, recon = self.model(X.to(self.device), padding_masks)  # (batch_size, padded_length, feat_dim)

            # Cascade noise masks (batch_size, padded_length, feat_dim) and padding masks (batch_size, padded_length)
            target_masks = target_masks * padding_masks.unsqueeze(-1)
            loss = self.loss_module(predictions, targets,
                                    target_masks)  # (num_active,) individual loss (square error per element) for each active value in batch
            batch_loss = torch.sum(loss)
            mean_loss = batch_loss / len(loss)  # mean loss (over active elements) used for optimization

            if self.l2_reg:
                total_loss = mean_loss + self.l2_reg * l2_reg_loss(self.model)
            else:
                total_loss = mean_loss

            # Zero gradients, perform a backward pass, and update the weights.
            self.optimizer.zero_grad()
            total_loss.backward()

            # torch.nn.utils.clip_grad_value_(self.model.parameters(), clip_value=1.0)
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=4.0)
            self.optimizer.step()

            metrics = {"loss": mean_loss.item()}
            if i % self.print_interval == 0:
                ending = "" if epoch_num is None else 'Epoch {} '.format(epoch_num)
                self.print_callback(i, metrics, prefix='Training ' + ending)

            with torch.no_grad():
                total_active_elements += len(loss)
                epoch_loss += batch_loss.item()  # add total loss of batch

        epoch_loss = epoch_loss / total_active_elements  # average loss per element for whole epoch
        self.epoch_metrics['epoch'] = epoch_num
        self.epoch_metrics['loss'] = epoch_loss
        return self.epoch_metrics

    def evaluate(self, epoch_num=None, keep_all=True):

        self.model = self.model.eval()

        epoch_loss = 0  # total loss of epoch
        total_active_elements = 0  # total unmasked elements in epoch

        if keep_all:
            per_batch = {'target_masks': [], 'targets': [], 'predictions': [], 'metrics': [], 'IDs': []}
        for i, batch in enumerate(self.dataloader):

            X, targets, target_masks, padding_masks, IDs = batch
            targets = targets.to(self.device)
            target_masks = target_masks.to(self.device)  # 1s: mask and predict, 0s: unaffected input (ignore)
            padding_masks = padding_masks.to(self.device)  # 0s: ignore

            # TODO: for debugging
            # input_ok = utils.check_tensor(X, verbose=False, zero_thresh=1e-8, inf_thresh=1e4)
            # if not input_ok:
            #     print("Input problem!")
            #     ipdb.set_trace()
            #
            # utils.check_model(self.model, verbose=False, stop_on_error=True)

            predictions, recon = self.model(X.to(self.device), padding_masks)  # (batch_size, padded_length, feat_dim)

            # Cascade noise masks (batch_size, padded_length, feat_dim) and padding masks (batch_size, padded_length)
            target_masks = target_masks * padding_masks.unsqueeze(-1)
            loss = self.loss_module(predictions, targets,
                                    target_masks)  # (num_active,) individual loss (square error per element) for each active value in batch
            batch_loss = torch.sum(loss).cpu().item()
            mean_loss = batch_loss / len(loss)  # mean loss (over active elements) used for optimization the batch

            if keep_all:
                per_batch['target_masks'].append(target_masks.cpu().numpy())
                per_batch['targets'].append(targets.cpu().numpy())
                per_batch['predictions'].append(predictions.cpu().numpy())
                per_batch['metrics'].append([loss.cpu().numpy()])
                per_batch['IDs'].append(IDs)

            metrics = {"loss": mean_loss}
            if i % self.print_interval == 0:
                ending = "" if epoch_num is None else 'Epoch {} '.format(epoch_num)
                self.print_callback(i, metrics, prefix='Evaluating ' + ending)

            total_active_elements += len(loss)
            epoch_loss += batch_loss  # add total loss of batch

        epoch_loss = epoch_loss / total_active_elements  # average loss per element for whole epoch
        self.epoch_metrics['epoch'] = epoch_num
        self.epoch_metrics['loss'] = epoch_loss

        if keep_all:
            return self.epoch_metrics, per_batch
        else:
            return self.epoch_metrics


class SupervisedRunner(BaseRunner):

    def __init__(self, *args, **kwargs):

        super(SupervisedRunner, self).__init__(*args, **kwargs)

        # if isinstance(args[3], torch.nn.CrossEntropyLoss):
        #     self.classification = True  # True if classification, False if regression
        #     self.analyzer = analysis.Analyzer(print_conf_mat=True)
        # else:
        #     self.classification = False

        self.classification = True  # True if classification, False if regression
        self.analyzer = analysis.Analyzer(print_conf_mat=True)

    def train_epoch(self, epoch_num=None):

        self.model = self.model.train()

        epoch_loss = 0  # total loss of epoch
        total_samples = 0  # total samples in epoch
        per_batch = {'predictions': [], 'targets': []}
        # self.loss_module = torch.nn.CrossEntropyLoss()
        for i, batch in enumerate(self.dataloader):
            X, targets, padding_masks, IDs = batch
            targets = targets.to(self.device)
            padding_masks = padding_masks.to(self.device)  # 0s: ignore
            # regression: (batch_size, num_labels); classification: (batch_size, num_classes) of logits
            predictions, recon, _ = self.model(X.to(self.device), padding_masks)

            loss = self.loss_module(predictions, targets)  # (batch_size,) loss for each sample in the batch
            batch_loss = torch.sum(loss)
            mean_loss = batch_loss / len(loss)  # mean loss (over samples) used for optimization

            # jointly loss
            # mean_loss, loss, batch_loss = self.loss_module(predictions, targets, recon.transpose(-1, 1),
            #                                                X.to(self.device))

            per_batch['predictions'].append(predictions.cpu().detach().numpy())
            per_batch['targets'].append(targets.cpu().numpy())

            if self.l2_reg:
                total_loss = mean_loss + self.l2_reg * l2_reg_loss(self.model)
            else:
                total_loss = mean_loss

            # Zero gradients, perform a backward pass, and update the weights.
            self.optimizer.zero_grad()
            total_loss.backward()

            # torch.nn.utils.clip_grad_value_(self.model.parameters(), clip_value=1.0)
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=4.0)
            self.optimizer.step()

            metrics = {"loss": mean_loss.item()}
            if i % self.print_interval == 0:
                ending = "" if epoch_num is None else 'Epoch {} '.format(epoch_num)
                self.print_callback(i, metrics, prefix='Training ' + ending)

            with torch.no_grad():
                total_samples += len(loss)
                epoch_loss += batch_loss.item()  # add total loss of batch

        epoch_loss = epoch_loss / total_samples  # average loss per sample for whole epoch
        self.epoch_metrics['epoch'] = epoch_num
        self.epoch_metrics['loss'] = epoch_loss

        all_samples = np.concatenate(per_batch['predictions'], axis=0)
        n_smaples = all_samples.shape[0]  # Select the first n_samples to calculate accuracy
        predictions = torch.from_numpy(np.concatenate(per_batch['predictions'], axis=0)[:n_smaples])
        probs = torch.nn.functional.softmax(
            predictions)  # (total_samples, num_classes) est. prob. for each class and sample
        predictions = torch.argmax(probs, dim=1).cpu().numpy()  # (total_samples,) int class index for each sample
        probs = probs.cpu().numpy()
        targets = np.concatenate(per_batch['targets'], axis=0)[:n_smaples].flatten()
        class_names = np.arange(probs.shape[1])  # TODO: temporary until I decide how to pass class names
        metrics_dict = self.analyzer.analyze_classification(predictions, targets, class_names)

        self.epoch_metrics['accuracy'] = metrics_dict['total_accuracy']  # same as average recall over all classes
        self.epoch_metrics['precision'] = metrics_dict['prec_avg']  # average precision over all classes
        self.epoch_metrics['recall'] = metrics_dict['rec_avg']
        self.epoch_metrics['f1'] = metrics_dict['f1_avg']
        # per class metrics
        for m in ['precision', 'recall', 'f1']:
            mval = metrics_dict[m]
            for i in range(len(mval)):
                heads = m + '-cls-' + str(i + 1)
                self.epoch_metrics[heads] = mval[i]

        return self.epoch_metrics, per_batch

    def evaluate(self, epoch_num=None, keep_all=True):

        self.model = self.model.eval()

        epoch_loss = 0  # total loss of epoch
        total_samples = 0  # total samples in epoch

        per_batch = {'target_masks': [], 'targets': [], 'predictions': [], 'recon': [], 'metrics': [], 'IDs': []}

        for i, batch in enumerate(self.dataloader):
            X, targets, padding_masks, IDs = batch

            # for u in range(X.shape[0]):
            #     plt.clf()
            #     plt.plot(X[u, :,0])
            #     plt.savefig('vis/input/' + str(u) + '.png', bbox_inches='tight')
            #
            # with open('vis/class.txt', 'w', encoding='utf8') as f:
            #     for u in range(targets.shape[0]):
            #         f.write(str(targets.numpy()[u,0]))
            #         f.write('\n')
            #
            # np.save('vis/inputs.npy',X.detach().cpu().numpy())
            # np.save('vis/label.npy',targets.detach().cpu().numpy())
            targets = targets.to(self.device)
            padding_masks = padding_masks.to(self.device)  # 0s: ignore
            # regression: (batch_size, num_labels); classification: (batch_size, num_classes) of logits
            predictions, recon, _ = self.model(X.to(self.device), padding_masks)

            """
            Loss value is not calculated at the time of validation. 20231015
            """
            loss = self.loss_module(predictions, targets)  # (batch_size,) loss for each sample in the batch
            batch_loss = torch.sum(loss).cpu().item()
            mean_loss = batch_loss / len(loss)  # mean loss (over samples)

            # jointly loss
            # mean_loss, loss, batch_loss = self.loss_module(predictions, targets, recon.transpose(-1, 1),
            #                                                X.to(self.device))

            per_batch['targets'].append(targets.cpu().numpy())
            per_batch['predictions'].append(predictions.cpu().detach().numpy())
            per_batch['recon'].append(recon.cpu().detach().numpy())
            # per_batch['metrics'].append([loss.cpu().numpy()])
            per_batch['IDs'].append(IDs)

            metrics = {"loss": mean_loss}
            if i % self.print_interval == 0:
                ending = "" if epoch_num is None else 'Epoch {} '.format(epoch_num)
                self.print_callback(i, metrics, prefix='Evaluating ' + ending)

            total_samples += len(loss)
            epoch_loss += batch_loss  # add total loss of batch

        epoch_loss = epoch_loss / total_samples  # average loss per element for whole epoch
        self.epoch_metrics['epoch'] = epoch_num
        self.epoch_metrics['loss'] = epoch_loss

        if self.classification:
            all_samples = np.concatenate(per_batch['predictions'], axis=0)
            n_smaples = all_samples.shape[0]  # Select the first n_samples to calculate accuracy
            predictions = torch.from_numpy(np.concatenate(per_batch['predictions'], axis=0)[:n_smaples])
            probs = torch.nn.functional.softmax(
                predictions)  # (total_samples, num_classes) est. prob. for each class and sample
            predictions = torch.argmax(probs, dim=1).cpu().numpy()  # (total_samples,) int class index for each sample
            probs = probs.cpu().numpy()
            targets = np.concatenate(per_batch['targets'], axis=0)[:n_smaples].flatten()
            class_names = np.arange(probs.shape[1])  # TODO: temporary until I decide how to pass class names
            metrics_dict = self.analyzer.analyze_classification(predictions, targets, class_names)

            self.epoch_metrics['accuracy'] = metrics_dict['total_accuracy']  # same as average recall over all classes
            self.epoch_metrics['precision'] = metrics_dict['prec_avg']  # average precision over all classes
            self.epoch_metrics['recall'] = metrics_dict['rec_avg']
            self.epoch_metrics['f1'] = metrics_dict['f1_avg']
            # per class metrics
            for m in ['precision', 'recall', 'f1']:
                mval = metrics_dict[m]
                for i in range(len(mval)):
                    heads = m + '-cls-' + str(i+1)
                    self.epoch_metrics[heads] = mval[i]

            if self.model.num_classes == 2:
                false_pos_rate, true_pos_rate, _ = sklearn.metrics.roc_curve(targets, probs[:, 1])  # 1D scores needed
                self.epoch_metrics['AUROC'] = sklearn.metrics.auc(false_pos_rate, true_pos_rate)

                prec, rec, _ = sklearn.metrics.precision_recall_curve(targets, probs[:, 1])
                self.epoch_metrics['AUPRC'] = sklearn.metrics.auc(rec, prec)

        if keep_all:
            return self.epoch_metrics, per_batch
        else:
            return self.epoch_metrics

    def make_weibull_from_trainsets(self):
        correct_features = []
        category_index = []
        with torch.no_grad():
            for i, batch in enumerate(self.dataloader):
                X, targets, padding_masks, IDs = batch

                # [1] Get model features
                targets = targets.squeeze().to(self.device)
                padding_masks = padding_masks.to(self.device)  # 0s: ignore
                logits, _, latent = self.model(X.to(self.device), padding_masks)

                squeezed_latent = []
                squeezed_latent.append(logits)
                for layer in latent:
                    m = nn.AdaptiveAvgPool1d(1)
                    new_layer = torch.squeeze(m(layer))
                    squeezed_latent.append(new_layer)

                #  the activation vector feature is  [16, 4(num_class)] +[16,32] +[16,32] =[16,68]
                feature = torch.cat(squeezed_latent, 1)

                predicted_category = torch.max(feature, dim=1)[1]
                idx = np.where(predicted_category.cpu().numpy() == targets.cpu().numpy())
                tags = predicted_category[idx]
                feat = feature[idx]
                correct_features.append(feat)  # right features
                category_index.append(tags)  # right labels

            correct_features = torch.cat(correct_features, 0)
            category_index = torch.cat(category_index, 0)
            unique_groups = torch.unique(category_index)
            grouped_dict = {}  # all class
            for group in unique_groups:
                indices = (category_index == group)
                grouped_dict[group.item()] = correct_features[indices]

            # [2] MAV compute
            MAV = []
            dists = []
            categories = []
            for key in grouped_dict:
                mav = torch.mean(grouped_dict[key], dim=0)
                MAV.append(mav)
                # [3] Compute Distances
                dist = openmax.compute_channel_distances(mav, grouped_dict[key])
                dists.append(dist)
                categories.append(key)
            # [4] Weibull Fitting
            weibull_model = openmax.weibull_tailfitting(MAV, dists, categories)
            return weibull_model

    def openmax(self, weibull_model):
        input_scores = []
        labels = []
        with torch.no_grad():
            for i, batch in enumerate(self.dataloader):
                X, targets, padding_masks, IDs = batch

                # [1] Get model features
                targets = targets.squeeze().to(self.device)
                padding_masks = padding_masks.to(self.device)  # 0s: ignore
                logits, _, latent = self.model(X.to(self.device), padding_masks)
                squeezed_latent = []
                squeezed_latent.append(logits)
                for layer in latent:
                    m = nn.AdaptiveAvgPool1d(1)
                    new_layer = torch.squeeze(m(layer))
                    squeezed_latent.append(new_layer)

                #  the activation vector feature is  [16, 4(num_class)] +[16,32] +[16,32] =[16,68]
                feature = torch.cat(squeezed_latent, 1)
                input_scores.append(feature)
                labels.append(targets)
        input_scores = torch.cat(input_scores, 0)
        labels = torch.cat(labels, dim=0).cpu().numpy()
        # [5] Computing OpenMax Scores
        num_classes = len(set(labels)) - 1
        # class_to_idx_dict = {"0": 0, "1": 1, "2": 2, "3": 3}
        class_to_idx_dict = {i: i for i in range(num_classes)}


        # we will now do N+1 class classification, we setup a open_set_class_to_idx_dict
        open_set_class_to_idx_dict = dict()
        open_set_class_to_idx_dict['unknown'] = num_classes  # the last class is the unknown class
        for k in class_to_idx_dict:
            open_set_class_to_idx_dict[k] = class_to_idx_dict[k]

        openmax_probs, softmax_probs = openmax.get_scores(weibull_model, input_scores, num_classes, class_to_idx_dict,
                                                          open_set_class_to_idx_dict)
        return openmax_probs, softmax_probs, labels

    def analysis_identify(self, y_pred, y_true, class_names):
        return openmax.analyze_classification(y_pred, y_true, class_names)