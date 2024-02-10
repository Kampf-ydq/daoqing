import logging

import matplotlib.pyplot as plt

from CNNFCN_PatternSeries.utils.utils import inverse_transformed_samples

logging.basicConfig(format='%(asctime)s | %(levelname)s : %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("Loading packages ...")
import os
import sys
import time
import pickle
import json

# 3rd party packages
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

# Project modules
from options import Options
from running import setup, pipeline_factory, validate, check_progress, NEG_METRICS
from utils import utils
from data_loader.data import data_factory, Normalizer
from data_loader.datasplit import split_dataset
from models.model_factory import model_factory
from models.loss import get_loss_module
from optimizers import get_optimizer
import numpy as np
import random

seed = 666
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
np.random.seed(seed)
random.seed(seed)
torch.backends.cudnn.deterministic = True


def main(config):
    total_epoch_time = 0
    total_eval_time = 0

    total_start_time = time.time()

    # Add file logging besides stdout
    file_handler = logging.FileHandler(os.path.join(config['output_dir'], 'output.log'))
    logger.addHandler(file_handler)

    logger.info('Running:\n{}\n'.format(' '.join(sys.argv)))  # command used to run

    if config['seed'] is not None:
        torch.manual_seed(config['seed'])

    device = torch.device('cuda' if (torch.cuda.is_available() and config['gpu'] != '-1') else 'cpu')
    logger.info("Using device: {}".format(device))
    # ##### CUDA 1 ##### #
    torch.cuda.set_device(1)
    if device.type == 'cuda':
        logger.info("Device index: {}".format(torch.cuda.current_device()))

    # Build data
    logger.info("Loading and preprocessing data ...")

    data_class = data_factory[config['data_class']]
    my_data = data_class(config['data_dir'], pattern=config['pattern'], n_proc=config['n_proc'],
                         limit_size=config['limit_size'], config=config)
    feat_dim = my_data.feature_df.shape[1]  # dimensionality of data features

    """
    Draw the time series. ydq 20231002 ditecting.
    """
    """
    dim0 = my_data.feature_df.iloc[:152, 0]
    win = 152
    plt.subplot(3, 1, 1)
    plt.plot(range(152), my_data.feature_df.iloc[:152, 0].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[:152, 1].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[:152, 2].values.tolist())
    plt.subplot(3, 1, 2)
    plt.plot(range(152), my_data.feature_df.iloc[152:304, 0].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[152:304, 1].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[152:304, 2].values.tolist())
    plt.subplot(3, 1, 3)
    plt.plot(range(152), my_data.feature_df.iloc[2*win:3*win, 0].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[2*win:3*win, 1].values.tolist())
    plt.plot(range(152), my_data.feature_df.iloc[2*win:3*win, 2].values.tolist())
    plt.show()
    """

    if config['task'] == 'classification':
        validation_method = 'StratifiedShuffleSplit'
        labels = my_data.labels_df.values.flatten()
    else:
        validation_method = 'ShuffleSplit'
        labels = None

    # Split dataset
    test_data = my_data
    test_indices = None  # will be converted to empty list in `split_dataset`, if also test_set_ratio == 0
    val_data = my_data
    val_indices = []
    if config['test_pattern']:  # used if test data come from different files / file patterns
        test_data = data_class(config['data_dir'], pattern=config['test_pattern'], n_proc=-1, config=config)
        test_indices = test_data.all_IDs
    if config[
        'test_from']:  # load test IDs directly from file, if available, otherwise use `test_set_ratio`. Can work together with `test_pattern`
        test_indices = list(set([line.rstrip() for line in open(config['test_from']).readlines()]))
        try:
            test_indices = [int(ind) for ind in test_indices]  # integer indices
        except ValueError:
            pass  # in case indices are non-integers
        logger.info("Loaded {} test IDs from file: '{}'".format(len(test_indices), config['test_from']))
    if config['val_pattern']:  # used if val data come from different files / file patterns
        val_data = data_class(config['data_dir'], pattern=config['val_pattern'], n_proc=-1, config=config)
        val_indices = val_data.all_IDs

    # Note: currently a validation set must exist, either with `val_pattern` or `val_ratio`
    # Using a `val_pattern` means that `val_ratio` == 0 and `test_ratio` == 0
    if config['val_ratio'] > 0:

        train_indices, val_indices, test_indices = split_dataset(data_indices=my_data.all_IDs,
                                                                 validation_method=validation_method,
                                                                 n_splits=1,
                                                                 validation_ratio=config['val_ratio'],
                                                                 test_set_ratio=config['test_ratio'],
                                                                 # used only if test_indices not explicitly specified
                                                                 test_indices=test_indices,
                                                                 random_seed=1337,
                                                                 labels=labels)

        train_indices = train_indices[0]  # `split_dataset` returns a list of indices *per fold/split*
        val_indices = val_indices[0]  # `split_dataset` returns a list of indices *per fold/split*
    else:
        train_indices = my_data.all_IDs
        if test_indices is None:
            test_indices = []

    logger.info("{} samples may be used for training".format(len(train_indices)))
    logger.info("{} samples will be used for validation".format(len(val_indices)))
    logger.info("{} samples will be used for testing".format(len(test_indices)))

    with open(os.path.join(config['output_dir'], 'data_indices.json'), 'w') as f:
        try:
            json.dump({'train_indices': list(map(int, train_indices)),
                       'val_indices': list(map(int, val_indices)),
                       'test_indices': list(map(int, test_indices))}, f, indent=4)
        except ValueError:  # in case indices are non-integers
            json.dump({'train_indices': list(train_indices),
                       'val_indices': list(val_indices),
                       'test_indices': list(test_indices)}, f, indent=4)

    # Pre-process features
    normalizer = None
    if config['norm_from']:
        with open(config['norm_from'], 'rb') as f:
            norm_dict = pickle.load(f)
        normalizer = Normalizer(**norm_dict)
    elif config['normalization'] is not None:
        normalizer = Normalizer(config['normalization'])
        my_data.feature_df.loc[train_indices] = normalizer.normalize(my_data.feature_df.loc[train_indices])
        if not config['normalization'].startswith('per_sample'):
            # get normalizing values from training set and store for future use
            norm_dict = normalizer.__dict__
            with open(os.path.join(config['output_dir'], 'normalization.pickle'), 'wb') as f:
                pickle.dump(norm_dict, f, pickle.HIGHEST_PROTOCOL)
    if normalizer is not None:
        if len(val_indices):
            val_data.feature_df.loc[val_indices] = normalizer.normalize(val_data.feature_df.loc[val_indices])
        if len(test_indices):
            test_data.feature_df.loc[test_indices] = normalizer.normalize(test_data.feature_df.loc[test_indices])

    # Create model
    logger.info("Creating model ...")
    model = model_factory(config, my_data)

    if config['freeze']:
        for name, param in model.named_parameters():
            if name.startswith('output_layer'):
                param.requires_grad = True
            else:
                param.requires_grad = False

    # Initialize optimizer
    if config['global_reg']:
        weight_decay = config['l2_reg']
        output_reg = None
    else:
        weight_decay = 0
        output_reg = config['l2_reg']

    optim_class = get_optimizer(config['optimizer'])
    optimizer = optim_class(model.parameters(), lr=config['lr'], weight_decay=weight_decay)

    start_epoch = 0
    lr_step = 0  # current step index of `lr_step`
    lr = config['lr']  # current learning step
    # Load model and optimizer state
    if args.load_model:
        model, optimizer, start_epoch = utils.load_model(model, config['load_model'], optimizer, config['resume'],
                                                         config['change_output'],
                                                         config['lr'],
                                                         config['lr_step'],
                                                         config['lr_factor'])
    model.to(device)

    loss_module = get_loss_module(config)

    # Initialize data generators
    dataset_class, collate_fn, runner_class = pipeline_factory(config)
    val_dataset = dataset_class(val_data, val_indices)

    val_loader = DataLoader(dataset=val_dataset,
                            batch_size=config['batch_size'],
                            shuffle=False,
                            num_workers=config['num_workers'],
                            pin_memory=True,
                            collate_fn=lambda x: collate_fn(x, max_len=model.max_len))

    train_dataset = dataset_class(my_data, train_indices)

    train_loader = DataLoader(dataset=train_dataset,
                              batch_size=config['batch_size'],
                              shuffle=True,
                              num_workers=config['num_workers'],
                              pin_memory=True,
                              collate_fn=lambda x: collate_fn(x, max_len=model.max_len))

    trainer = runner_class(model, train_loader, device, loss_module, optimizer, l2_reg=output_reg,
                           print_interval=config['print_interval'], console=config['console'])
    val_evaluator = runner_class(model, val_loader, device, loss_module,
                                 print_interval=config['print_interval'], console=config['console'])

    tensorboard_writer = SummaryWriter(config['tensorboard_dir'])

    best_value = 1e16 if config[
                             'key_metric'] in NEG_METRICS else -1e16  # initialize with +inf or -inf depending on key metric
    val_metrics = []  # (for validation) list of lists: for each epoch, stores metrics like loss, ...
    best_metrics = {}
    train_metrics = []

    if config['test_only'] == 'testset':  # Only evaluate and skip training
        model_path = os.path.join(config['save_dir'], 'model_{}.pth'.format('last'))
        classifier, _, _ = utils.load_model(model, model_path, optimizer, config['resume'],
                                            config['change_output'],
                                            config['lr'],
                                            config['lr_step'],
                                            config['lr_factor'])
        dataset_class, collate_fn, runner_class = pipeline_factory(config)
        # test_dataset = dataset_class(test_data, test_indices)
        test_dataset = dataset_class(val_data, val_indices)
        test_loader = DataLoader(dataset=test_dataset,
                                 batch_size=config['batch_size'],
                                 shuffle=False,
                                 num_workers=config['num_workers'],
                                 pin_memory=True,
                                 collate_fn=lambda x: collate_fn(x, max_len=model.max_len))
        test_evaluator = runner_class(classifier, test_loader, device, loss_module,
                                      print_interval=config['print_interval'], console=config['console'])

        aggr_metrics_test, per_batch_test = test_evaluator.evaluate(keep_all=True)
        print_str = 'Test Summary: '
        for k, v in aggr_metrics_test.items():
            if v is None: v = 1
            print_str += '{}: {:8f} | '.format(k, v)
        logger.info(print_str)

        """
        # [Open Set Identify]
        # fits per class weibull models on the distances in the training set
        weibull_model = trainer.make_weibull_from_trainsets()
        so, ss, y_true = test_evaluator.openmax(weibull_model)
        # # Fusion of threshold judgment methods for classification
        # pred_openmax = []
        # thr = 0.9
        # # Failure to meet the conditions is considered to be category 0
        # pred_openmax.append(np.argmax(so) if np.max(so) >= thr else 0)
        # pred_openmax = np.array(pred_openmax)


        pred_openmax = np.argmax(so, axis=1)
        pred_softmax = np.argmax(ss, axis=1)
        class_names = np.arange(so.shape[1])
        metrics_dict_opx = test_evaluator.analysis_identify(pred_openmax, y_true, class_names)
        metrics_dict_sfx = test_evaluator.analysis_identify(pred_softmax, y_true, class_names)
        logger.info('OpenMax: Avg Precision {}, Avg Recall {} // Precision {} | Recall {} | F1/4 {}'.format(metrics_dict_opx['prec_avg'], metrics_dict_opx['rec_avg'],
                                                                                                             metrics_dict_opx['precision'],
                                                                                                             metrics_dict_opx['recall'],
                                                                                                             metrics_dict_opx['f1/4']))
        logger.info('SoftMax: Avg Precision {}, Avg Recall {} // Precision {} | Recall {} | F1/4 {}'.format(metrics_dict_sfx['prec_avg'], metrics_dict_sfx['rec_avg'],
                                                                                                                metrics_dict_sfx['precision'],
                                                                                                                     metrics_dict_sfx['recall'],
                                                                                                                     metrics_dict_sfx['f1/4']))
        """
        return

    # Evaluate on validation before training
    aggr_metrics_val, best_metrics, best_value, _ = validate(val_evaluator, tensorboard_writer, config, best_metrics,
                                                             best_value, epoch=0)
    metrics_names, metrics_values = zip(*aggr_metrics_val.items())
    val_metrics.append(list(metrics_values))

    logger.info('Starting training Classifier...')
    logger.info("\t\t\t    ======")
    logger.info("\t\t\t  //     ")
    logger.info("\t\t\t ||      ")
    logger.info("\t\t\t  \\\\     ")
    logger.info("\t\t\t     =====")
    for epoch in tqdm(range(start_epoch + 1, config["epochs"] + 1), desc='Training Epoch', leave=False):
        mark = epoch if config['save_all'] else 'last'
        epoch_start_time = time.time()
        aggr_metrics_train, _ = trainer.train_epoch(epoch)  # dictionary of aggregate epoch metrics

        _, metrics_values = zip(*aggr_metrics_train.items())
        train_metrics.append(list(metrics_values))

        epoch_runtime = time.time() - epoch_start_time
        print()
        print_str = 'Epoch {} Training Summary: '.format(epoch)
        for k, v in aggr_metrics_train.items():
            tensorboard_writer.add_scalar('{}/train'.format(k), v, epoch)
            print_str += '{}: {:8f} | '.format(k, v)
        logger.info(print_str)
        logger.info("Epoch runtime: {} hours, {} minutes, {} seconds\n".format(*utils.readable_time(epoch_runtime)))
        total_epoch_time += epoch_runtime
        avg_epoch_time = total_epoch_time / (epoch - start_epoch)
        avg_batch_time = avg_epoch_time / len(train_loader)
        avg_sample_time = avg_epoch_time / len(train_dataset)
        logger.info(
            "Avg epoch train. time: {} hours, {} minutes, {} seconds".format(*utils.readable_time(avg_epoch_time)))
        logger.info("Avg batch train. time: {} seconds".format(avg_batch_time))
        logger.info("Avg sample train. time: {} seconds".format(avg_sample_time))

        aggr_metrics_val, best_metrics, best_value, _ = validate(val_evaluator, tensorboard_writer, config,
                                                                 best_metrics, best_value, epoch)
        metrics_names, metrics_values = zip(*aggr_metrics_val.items())
        val_metrics.append(list(metrics_values))
        # evaluate if first or last epoch or at specified interval
        """
        if (epoch == config["epochs"]) or (epoch == start_epoch + 1) or (epoch % config['val_interval'] == 0):
            aggr_metrics_val, best_metrics, best_value, _ = validate(val_evaluator, tensorboard_writer, config,
                                                                     best_metrics, best_value, epoch)
            metrics_names, metrics_values = zip(*aggr_metrics_val.items())
            val_metrics.append(list(metrics_values))

            # Data distribution analysis. ydq 20231014.
            if epoch == 96:
                # get last epoch's Softmax outputs for PDF and CDF analysis.
                _, _, _, per_batch = validate(val_evaluator, tensorboard_writer, config,
                                              best_metrics, best_value, epoch)
                predictions = torch.from_numpy(np.concatenate(per_batch['predictions'], axis=0))
                logger.info('Analysis PDF and CDF about SOFTMAX layer outputs.')
                from math_analysis import pdf_and_cdf
                # pdf_and_cdf.plot_prob_function(predictions)
        """

        utils.save_model(os.path.join(config['save_dir'], 'model_{}.pth'.format(mark)), epoch, model, optimizer)

        # Learning rate scheduling
        if epoch == config['lr_step'][lr_step]:
            utils.save_model(os.path.join(config['save_dir'], 'model_{}.pth'.format(epoch)), epoch, model, optimizer)
            lr = lr * config['lr_factor'][lr_step]
            if lr_step < len(config['lr_step']) - 1:  # so that this index does not get out of bounds
                lr_step += 1
            logger.info('Learning rate updated to: ', lr)
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr

        # Difficulty scheduling
        if config['harden'] and check_progress(epoch):
            train_loader.dataset.update()
            val_loader.dataset.update()

    # Export evolution of metrics over epochs
    header = metrics_names
    metrics_filepath_val = os.path.join(config["output_dir"], "metrics_val_" + config["experiment_name"] + ".xls")
    utils.export_performance_metrics(metrics_filepath_val, val_metrics, header, sheet_name="metrics")
    metrics_filepath_tra = os.path.join(config["output_dir"], "metrics_train_" + config["experiment_name"] + ".xls")
    utils.export_performance_metrics(metrics_filepath_tra, train_metrics, header, sheet_name="metrics")

    # Export record metrics to a file accumulating records from all experiments
    utils.register_record(config["records_file"], config["initial_timestamp"], config["experiment_name"],
                          best_metrics, aggr_metrics_val, comment=config['comment'])

    logger.info('Best {} was {}. Other metrics: {}'.format(config['key_metric'], best_value, best_metrics))
    logger.info('All Done!')

    total_runtime = time.time() - total_start_time
    logger.info("Total runtime: {} hours, {} minutes, {} seconds\n".format(*utils.readable_time(total_runtime)))

    return best_value


if __name__ == '__main__':
    args = Options().parse()  # `argsparse` object
    config = setup(args)  # configuration dictionary

    main(config)
