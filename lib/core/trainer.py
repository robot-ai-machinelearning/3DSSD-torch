import warnings

warnings.filterwarnings('ignore')

import argparse
import datetime
import os
import sys
import time

import numpy as np
import torch
import torch.optim as optim
import tqdm
from tensorboardX import SummaryWriter
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from lib.core.config import (assert_and_infer_cfg, cfg, cfg_from_file,
                             cfg_from_list)
from lib.core.trainer_utils import (LRScheduler, checkpoint_state,
                                    save_checkpoint)
from lib.dataset.dataloader import choose_dataset
from lib.modeling import choose_model
from lib.modeling.single_stage_detector import compute_loss, post_process
from lib.utils.common_util import create_logger


# TODO: add bn_decay


# Init datasets and dataloaders
def my_worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)


def parse_args():
    parser = argparse.ArgumentParser(description='Trainer')
    parser.add_argument('--cfg', required=True, help='Config file for training')
    parser.add_argument('--output_dir', default=None, help='Dir to save log and ckpts')
    parser.add_argument('--restore_model_path', default=None,
                        help='Restore model path e.g. log/model.ckpt [default: None]')
    parser.add_argument('--img_list', default='train', help='Train/Val/Trainval list')
    parser.add_argument('--split', default='training', help='Dataset split')
    args = parser.parse_args()

    return args


class trainer:

    def __init__(self, args):
        self.batch_size = cfg.TRAIN.CONFIG.BATCH_SIZE
        self.gpu_num = cfg.TRAIN.CONFIG.GPU_NUM
        self.num_workers = cfg.DATA_LOADER.NUM_THREADS
        self.log_dir = cfg.MODEL.PATH.CHECKPOINT_DIR
        self.max_iteration = cfg.TRAIN.CONFIG.MAX_ITERATIONS
        self.total_epochs = cfg.TRAIN.CONFIG.TOTAL_EPOCHS
        self.checkpoint_interval = cfg.TRAIN.CONFIG.CHECKPOINT_INTERVAL
        self.summary_interval = cfg.TRAIN.CONFIG.SUMMARY_INTERVAL
        self.trainable_param_prefix = cfg.TRAIN.CONFIG.TRAIN_PARAM_PREFIX
        self.trainable_loss_prefix = cfg.TRAIN.CONFIG.TRAIN_LOSS_PREFIX
        if args.output_dir is not None:
            self.log_dir = args.output_dir

        self.restore_model_path = args.restore_model_path
        self.is_training = True

        # gpu_num
        self.gpu_num = min(self.gpu_num, torch.cuda.device_count())

        # save dir
        datetime_str = str(datetime.datetime.now())
        datetime_str = datetime_str[0:datetime_str.find(' ')] + '_' + datetime_str[datetime_str.find(' ') + 1:]
        self.log_dir = os.path.join(self.log_dir, datetime_str)
        if not os.path.exists(self.log_dir): os.makedirs(self.log_dir)
        self.logger = create_logger(os.path.join(self.log_dir, 'log_train.txt'))
        self.logger.info(str(args) + '\n')
        self.logger.info('**** Saving models to the path %s ****' % self.log_dir)
        self.logger.info('**** Saving configure file in %s ****' % self.log_dir)
        os.system('cp \"%s\" \"%s\"' % (args.cfg, self.log_dir))
        self.ckpt_dir = os.path.join(self.log_dir, 'ckpt')
        os.mkdir(self.ckpt_dir)

        # dataset
        dataset_func = choose_dataset()
        self.dataset = dataset_func('loading', split=args.split, img_list=args.img_list, is_training=self.is_training)
        self.val_dataset = dataset_func('loading', split=args.split, img_list='val', is_training=False)
        self.dataloader = DataLoader(self.dataset, batch_size=self.batch_size * self.gpu_num,
                                     shuffle=True, num_workers=self.num_workers,
                                     worker_init_fn=my_worker_init_fn,
                                     collate_fn=self.dataset.load_batch)
        self.val_dataloader = DataLoader(self.val_dataset, batch_size=1, shuffle=False,
                                         num_workers=self.num_workers,
                                         worker_init_fn=my_worker_init_fn,
                                         collate_fn=self.val_dataset.load_batch)

        self.logger.info('**** Dataset length is %d ****' % len(self.dataset))
        self.logger.info('**** Val Dataset length is %d ****' % len(self.val_dataset))

        # models
        self.model_func = choose_model()
        self.model = self.model_func(self.batch_size, self.is_training)
        self.model = self.model.cuda()

        torch.save(self.model, 'test.pth')

        # tensorboard
        self.tb_log = SummaryWriter(log_dir=os.path.join(self.log_dir, 'tensorboard'))

        # optimizer
        self.optimizer = optim.AdamW(self.model.parameters(), lr=cfg.SOLVER.BASE_LR, weight_decay=0)
        self.lr_scheduler = LRScheduler(self.optimizer)

        # load from checkpoint
        start_epoch = it = 0
        if args.restore_model_path is not None:
            it, start_epoch = self.model.load_params_with_optimizer(args.restore_model_path, to_cpu=False,
                                                                    optimizer=self.optimizer,
                                                                    logger=self.logger)
        self.start_epoch = start_epoch
        self.it = it

        if self.gpu_num > 1:
            self.logger.info("Use %d GPUs!" % self.gpu_num)
            self.model = torch.nn.DataParallel(self.model)

    def train_one_epoch(self, accumulated_iter, tbar, leave_pbar):
        total_it_each_epoch = len(self.dataloader)
        pbar = tqdm.tqdm(total=total_it_each_epoch, leave=leave_pbar, desc='train', dynamic_ncols=True)

        for batch_idx, batch_data_label in enumerate(self.dataloader):

            end_points = dict()

            for key in batch_data_label:
                if isinstance(batch_data_label[key], torch.Tensor):
                    batch_data_label[key] = batch_data_label[key].cuda()

            self.model.train()
            self.optimizer.zero_grad()

            try:
                cur_lr = float(self.optimizer.lr)
            except:
                cur_lr = self.optimizer.param_groups[0]['lr']

            end_points = self.model(batch_data_label)
            total_loss, disp_dict = compute_loss(end_points)
            total_loss.backward()
            total_norm = clip_grad_norm_(self.model.parameters(), 5.0)

            self.optimizer.step()

            if self.tb_log is not None:
                self.tb_log.add_scalar('learning_rate', cur_lr, accumulated_iter)
                self.tb_log.add_scalar('norm_grad', total_norm, accumulated_iter)
                for key in disp_dict:
                    if 'loss' in key:
                        self.tb_log.add_scalar(key, disp_dict[key].item(), accumulated_iter)

            accumulated_iter += 1

            pbar.update()
            pbar.set_postfix(dict(total_it=accumulated_iter))
            tbar.set_postfix({'total_loss': disp_dict['total_loss'].item()})
            tbar.refresh()

        pbar.close()
        return accumulated_iter

    def train(self, rank=0):
        accumulated_iter = self.it
        with tqdm.trange(self.start_epoch, self.total_epochs, desc='epochs', dynamic_ncols=True, leave=True) as tbar:
            for cur_epoch in tbar:

                # adjust learning rate based on epochs
                self.lr_scheduler.step(cur_epoch)

                accumulated_iter = self.train_one_epoch(
                    accumulated_iter=accumulated_iter, tbar=tbar, leave_pbar=(cur_epoch + 1 == self.total_epochs)
                )

                # save trained model
                self.trained_epoch = cur_epoch + 1
                if self.trained_epoch % 1 == 0 and rank == 0:
                    ckpt_name = os.path.join(self.ckpt_dir, 'checkpoint_epoch_{}'.format(self.trained_epoch))
                    save_checkpoint(
                        checkpoint_state(self.model, self.optimizer, self.trained_epoch, accumulated_iter),
                        filename=ckpt_name,
                    )

                # evaluation
                if self.trained_epoch % 1 == 0:
                    self.val_one_epoch()

    def val_one_epoch(self):
        # evaluate on val dataset for one epoch

        with torch.no_grad():

            progress_bar = tqdm.tqdm(total=len(self.val_dataloader), leave=True, desc='eval', dynamic_ncols=True)

            for batch_idx, batch_data_label in enumerate(self.val_dataloader):

                for key in batch_data_label:
                    if isinstance(batch_data_label[key], torch.Tensor):
                        batch_data_label[key] = batch_data_label[key].cuda()

                self.model.eval()
                end_points = self.model(batch_data_label)
                end_points = post_process(end_points)

                det_anno, gt_anno = self.dataset.generate_annotations(batch_data_label, end_points,
                                                                      self.val_dataloader.cls_list,
                                                                      save_to_file=False,
                                                                      output_dir=None)
                det_annos = det_annos + det_anno
                gt_annos = gt_annos + gt_anno

                progress_bar.update()
        progress_bar.close()

        self.logger.info('*************** Performance of %s *****************' % self.trained_epoch)
        result_str, result_dict = self.dataset.evaluation(det_annos, gt_annos)

        self.logger.info(result_str)
        self.logger.info('****************Evaluation done.*****************')


if __name__ == '__main__':
    torch.backends.cudnn.enabled = True
    torch.autograd.set_detect_anomaly(True)

    args = parse_args()
    cfg_from_file(args.cfg)

    cur_trainer = trainer(args)
    cur_trainer.train()
    print("**** Finish training steps ****")
