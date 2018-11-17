import argparse
import datetime
import glob
import os
import shutil
import sys
import time
from math import ceil

import numpy as np
import psutil
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tensorboardX import SummaryWriter
from torch.optim.lr_scheduler import CosineAnnealingLR, ReduceLROnPlateau
from torch.utils.data import DataLoader

from dataset import TrainDataProvider, TrainDataset, TestData, TestDataset
from metrics import accuracy, mapk, FocalLoss, CceCenterLoss
from metrics.smooth_topk_loss.svm import SmoothSVM
from models import ResNet, SimpleCnn, ResidualCnn, FcCnn, HcFcCnn, MobileNetV2, Drn, SeNet, NasNet, SeResNext50Cs, \
    StackNet
from models.ensemble import Ensemble
from swa_utils import moving_average
from utils import get_learning_rate, str2bool

cudnn.enabled = True
cudnn.benchmark = True

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def create_model(type, input_size, num_classes):
    if type == "resnet":
        model = ResNet(num_classes=num_classes)
    elif type in ["seresnext50", "seresnext101", "seresnet50", "seresnet101", "seresnet152", "senet154"]:
        model = SeNet(type=type, num_classes=num_classes)
    elif type == "nasnet":
        model = NasNet(num_classes=num_classes)
    elif type == "cnn":
        model = SimpleCnn(num_classes=num_classes)
    elif type == "residual_cnn":
        model = ResidualCnn(num_classes=num_classes)
    elif type == "fc_cnn":
        model = FcCnn(num_classes=num_classes)
    elif type == "hc_fc_cnn":
        model = HcFcCnn(num_classes=num_classes)
    elif type == "mobilenetv2":
        model = MobileNetV2(input_size=input_size, n_class=num_classes)
    elif type in ["drn_d_38", "drn_d_54", "drn_d_105"]:
        model = Drn(type=type, num_classes=num_classes)
    elif type == "seresnext50_cs":
        model = SeResNext50Cs(num_classes=num_classes)
    elif type == "stack":
        model = StackNet(num_classes=num_classes)
    else:
        raise Exception("Unsupported model type: '{}".format(type))

    return nn.DataParallel(model)


def zero_item_tensor():
    return torch.tensor(0.0).float().to(device, non_blocking=True)


def evaluate(model, data_loader, criterion, mapk_topk):
    model.eval()

    loss_sum_t = zero_item_tensor()
    mapk_sum_t = zero_item_tensor()
    accuracy_top1_sum_t = zero_item_tensor()
    accuracy_top3_sum_t = zero_item_tensor()
    accuracy_top5_sum_t = zero_item_tensor()
    accuracy_top10_sum_t = zero_item_tensor()
    step_count = 0

    with torch.no_grad():
        for batch in data_loader:
            images, categories = \
                batch[0].to(device, non_blocking=True), \
                batch[1].to(device, non_blocking=True)

            prediction_logits = model(images)
            loss = criterion(prediction_logits, categories)

            loss_sum_t += loss
            mapk_sum_t += mapk(prediction_logits, categories, topk=mapk_topk)
            accuracy_top1_sum_t += accuracy(prediction_logits, categories, topk=1)
            accuracy_top3_sum_t += accuracy(prediction_logits, categories, topk=3)
            accuracy_top5_sum_t += accuracy(prediction_logits, categories, topk=5)
            accuracy_top10_sum_t += accuracy(prediction_logits, categories, topk=10)

            step_count += 1

    loss_avg = loss_sum_t.item() / step_count
    mapk_avg = mapk_sum_t.item() / step_count
    accuracy_top1_avg = accuracy_top1_sum_t.item() / step_count
    accuracy_top3_avg = accuracy_top3_sum_t.item() / step_count
    accuracy_top5_avg = accuracy_top5_sum_t.item() / step_count
    accuracy_top10_avg = accuracy_top10_sum_t.item() / step_count

    return loss_avg, mapk_avg, accuracy_top1_avg, accuracy_top3_avg, accuracy_top5_avg, accuracy_top10_avg


def create_criterion(loss_type, num_classes):
    if loss_type == "cce":
        criterion = nn.CrossEntropyLoss()
    elif loss_type == "focal":
        criterion = FocalLoss()
    elif loss_type == "topk_svm":
        criterion = SmoothSVM(n_classes=num_classes, k=3, tau=1., alpha=1.)
    elif loss_type == "center":
        criterion = CceCenterLoss(num_classes=num_classes, alpha=0.5)
    else:
        raise Exception("Unsupported loss type: '{}".format(loss_type))
    return criterion


def create_optimizer(type, model, lr):
    if type == "adam":
        return optim.Adam(model.parameters(), lr=lr)
    elif type == "sgd":
        return optim.SGD(model.parameters(), lr=lr, weight_decay=1e-4, momentum=0.9, nesterov=True)
    else:
        raise Exception("Unsupported optimizer type: '{}".format(type))


def predict(model, data_loader, categories, tta=False):
    categories = np.array([c.replace(" ", "_") for c in categories])

    model.eval()

    all_predictions = []
    predicted_words = []
    with torch.no_grad():
        for batch in data_loader:
            images = batch[0].to(device, non_blocking=True)

            if tta:
                predictions1 = F.softmax(model(images), dim=1)
                predictions2 = F.softmax(model(images.flip(3)), dim=1)
                predictions = 0.5 * (predictions1 + predictions2)
            else:
                predictions = F.softmax(model(images), dim=1)

            _, prediction_categories = predictions.topk(3, dim=1, sorted=True)

            all_predictions.extend(predictions.cpu().data.numpy())
            predicted_words.extend([" ".join(categories[pc.cpu().data.numpy()]) for pc in prediction_categories])

    return all_predictions, predicted_words


def calculate_confusion(model, data_loader, num_categories, scale=True):
    confusion = np.zeros((num_categories, num_categories), dtype=np.float32)

    model.eval()

    all_predictions = []
    with torch.no_grad():
        for batch in data_loader:
            images, categories = \
                batch[0].to(device, non_blocking=True), \
                batch[1].to(device, non_blocking=True)

            predictions = F.softmax(model(images), dim=1)
            _, prediction_categories = predictions.topk(3, dim=1, sorted=True)

            for bpc, bc in zip(prediction_categories[:, 0], categories):
                confusion[bpc, bc] += 1

            all_predictions.extend(predictions.cpu().data.numpy())

    if scale:
        for c in range(confusion.shape[0]):
            category_count = confusion[c, :].sum()
            if category_count != 0:
                confusion[c, :] /= category_count

    return confusion, all_predictions


def find_sorted_model_files(base_dir):
    return sorted(glob.glob("{}/model-*.pth".format(base_dir)), key=lambda e: int(os.path.basename(e)[6:-4]))


def load_ensemble_model(base_dir, ensemble_model_count, data_loader, criterion, model_type, input_size, num_classes):
    ensemble_model_candidates = find_sorted_model_files(base_dir)[-(2 * ensemble_model_count):]
    if os.path.isfile("{}/swa_model.pth".format(base_dir)):
        ensemble_model_candidates.append("{}/swa_model.pth".format(base_dir))

    score_to_model = {}
    for model_file_path in ensemble_model_candidates:
        model_file_name = os.path.basename(model_file_path)
        model = create_model(type=model_type, input_size=input_size, num_classes=num_classes).to(device)
        model.load_state_dict(torch.load(model_file_path, map_location=device))

        val_loss_avg, val_mapk_avg, _, _, _, _ = evaluate(model, data_loader, criterion, 3)
        print("ensemble '%s': val_loss=%.4f, val_mapk=%.4f" % (model_file_name, val_loss_avg, val_mapk_avg))

        if len(score_to_model) < ensemble_model_count or min(score_to_model.keys()) < val_mapk_avg:
            if len(score_to_model) >= ensemble_model_count:
                del score_to_model[min(score_to_model.keys())]
            score_to_model[val_mapk_avg] = model

    ensemble = Ensemble(list(score_to_model.values()))

    val_loss_avg, val_mapk_avg, _, _, _, _ = evaluate(ensemble, data_loader, criterion, 3)
    print("ensemble: val_loss=%.4f, val_mapk=%.4f" % (val_loss_avg, val_mapk_avg))

    return ensemble


def main():
    args = argparser.parse_args()
    print("Arguments:")
    for arg in vars(args):
        print("  {}: {}".format(arg, getattr(args, arg)))
    print()

    input_dir = args.input_dir
    output_dir = args.output_dir
    base_model_dir = args.base_model_dir
    image_size = args.image_size
    augment = args.augment
    use_dummy_image = args.use_dummy_image
    use_progressive_image_sizes = args.use_progressive_image_sizes
    progressive_image_size_min = args.progressive_image_size_min
    progressive_image_size_step = args.progressive_image_size_step
    progressive_image_epoch_step = args.progressive_image_epoch_step
    batch_size = args.batch_size
    batch_iterations = args.batch_iterations
    test_size = args.test_size
    fold = args.fold
    train_on_unrecognized = args.train_on_unrecognized
    confusion_set = args.confusion_set
    num_category_shards = args.num_category_shards
    category_shard = args.category_shard
    eval_train_mapk = args.eval_train_mapk
    mapk_topk = args.mapk_topk
    num_shard_preload = args.num_shard_preload
    num_shard_loaders = args.num_shard_loaders
    num_workers = args.num_workers
    pin_memory = args.pin_memory
    epochs_to_train = args.epochs
    lr_scheduler_type = args.lr_scheduler
    lr_patience = args.lr_patience
    lr_min = args.lr_min
    lr_max = args.lr_max
    lr_min_decay = args.lr_min_decay
    lr_max_decay = args.lr_max_decay
    optimizer_type = args.optimizer
    loss_type = args.loss
    loss2_type = args.loss2
    loss2_start_sgdr_cycle = args.loss2_start_sgdr_cycle
    model_type = args.model
    patience = args.patience
    sgdr_cycle_epochs = args.sgdr_cycle_epochs
    sgdr_cycle_epochs_mult = args.sgdr_cycle_epochs_mult
    sgdr_cycle_end_prolongation = args.sgdr_cycle_end_prolongation
    sgdr_cycle_end_patience = args.sgdr_cycle_end_patience
    max_sgdr_cycles = args.max_sgdr_cycles

    use_extended_stroke_channels = model_type in ["cnn", "residual_cnn", "fc_cnn", "hc_fc_cnn"]
    print("use_extended_stroke_channels: {}".format(use_extended_stroke_channels), flush=True)

    progressive_image_sizes = list(range(progressive_image_size_min, image_size + 1, progressive_image_size_step))

    train_data_provider = TrainDataProvider(
        input_dir,
        50,
        num_shard_preload=num_shard_preload,
        num_workers=num_shard_loaders,
        test_size=test_size,
        fold=fold,
        train_on_unrecognized=train_on_unrecognized,
        confusion_set=confusion_set,
        num_category_shards=num_category_shards,
        category_shard=category_shard)

    train_data = train_data_provider.get_next()

    train_set = TrainDataset(train_data.train_set_df, image_size, use_extended_stroke_channels, augment,
                             use_dummy_image)
    train_set_data_loader = \
        DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=num_workers, pin_memory=pin_memory)

    val_set = TrainDataset(train_data.val_set_df, image_size, use_extended_stroke_channels, False, use_dummy_image)
    val_set_data_loader = \
        DataLoader(val_set, batch_size=batch_size, shuffle=False, num_workers=num_workers, pin_memory=pin_memory)

    if base_model_dir:
        for model_file_path in glob.glob("{}/model*.pth".format(base_model_dir)):
            shutil.copyfile(model_file_path, "{}/{}".format(output_dir, os.path.basename(model_file_path)))
        model = create_model(type=model_type, input_size=image_size, num_classes=len(train_data.categories)).to(device)
        model.load_state_dict(torch.load("{}/model.pth".format(output_dir), map_location=device))
    else:
        model = create_model(type=model_type, input_size=image_size, num_classes=len(train_data.categories)).to(device)

    torch.save(model.state_dict(), "{}/model.pth".format(output_dir))

    ensemble_model_index = 0
    for model_file_path in glob.glob("{}/model-*.pth".format(output_dir)):
        model_file_name = os.path.basename(model_file_path)
        model_index = int(model_file_name.replace("model-", "").replace(".pth", ""))
        ensemble_model_index = max(ensemble_model_index, model_index + 1)

    epoch_iterations = ceil(len(train_set) / batch_size)

    print("train_set_samples: {}, val_set_samples: {}".format(len(train_set), len(val_set)), flush=True)
    print()

    global_val_mapk_best_avg = float("-inf")
    sgdr_cycle_val_mapk_best_avg = float("-inf")

    optimizer = create_optimizer(optimizer_type, model, lr_max)
    lr_scheduler = CosineAnnealingLR(optimizer, T_max=sgdr_cycle_epochs, eta_min=lr_min)

    optim_summary_writer = SummaryWriter(log_dir="{}/logs/optim".format(output_dir))
    train_summary_writer = SummaryWriter(log_dir="{}/logs/train".format(output_dir))
    val_summary_writer = SummaryWriter(log_dir="{}/logs/val".format(output_dir))

    current_sgdr_cycle_epochs = sgdr_cycle_epochs
    sgdr_next_cycle_end_epoch = current_sgdr_cycle_epochs + sgdr_cycle_end_prolongation
    sgdr_iterations = 0
    sgdr_cycle_count = 0
    batch_count = 0
    epoch_of_last_improval = 0

    lr_scheduler_plateau = ReduceLROnPlateau(optimizer, mode="max", min_lr=lr_min, patience=lr_patience, factor=0.8)

    print('{"chart": "best_val_mapk", "axis": "epoch"}')
    print('{"chart": "val_mapk", "axis": "epoch"}')
    print('{"chart": "val_loss", "axis": "epoch"}')
    print('{"chart": "val_accuracy@1", "axis": "epoch"}')
    print('{"chart": "val_accuracy@3", "axis": "epoch"}')
    print('{"chart": "val_accuracy@5", "axis": "epoch"}')
    print('{"chart": "val_accuracy@10", "axis": "epoch"}')
    print('{"chart": "sgdr_cycle", "axis": "epoch"}')
    print('{"chart": "mapk", "axis": "epoch"}')
    print('{"chart": "loss", "axis": "epoch"}')
    print('{"chart": "lr_scaled", "axis": "epoch"}')
    print('{"chart": "mem_used", "axis": "epoch"}')
    print('{"chart": "epoch_time", "axis": "epoch"}')

    train_start_time = time.time()

    criterion = create_criterion(loss_type, len(train_data.categories))

    if loss_type == "center":
        optimizer_centloss = torch.optim.SGD(criterion.center.parameters(), lr=0.01)

    for epoch in range(epochs_to_train):
        epoch_start_time = time.time()

        print("memory used: {:.2f} GB".format(psutil.virtual_memory().used / 2 ** 30), flush=True)

        if use_progressive_image_sizes:
            next_image_size = \
                progressive_image_sizes[min(epoch // progressive_image_epoch_step, len(progressive_image_sizes) - 1)]

            if train_set.image_size != next_image_size:
                print("changing image size to {}".format(next_image_size), flush=True)
                train_set.image_size = next_image_size
                val_set.image_size = next_image_size

        model.train()

        train_loss_sum_t = zero_item_tensor()
        train_mapk_sum_t = zero_item_tensor()

        epoch_batch_iter_count = 0

        for b, batch in enumerate(train_set_data_loader):
            images, categories = \
                batch[0].to(device, non_blocking=True), \
                batch[1].to(device, non_blocking=True)

            if lr_scheduler_type == "cosine_annealing":
                lr_scheduler.step(epoch=min(current_sgdr_cycle_epochs, sgdr_iterations / epoch_iterations))

            if b % batch_iterations == 0:
                optimizer.zero_grad()

            prediction_logits = model(images)
            loss = criterion(prediction_logits, categories)
            loss.backward()

            with torch.no_grad():
                train_loss_sum_t += loss
                if eval_train_mapk:
                    train_mapk_sum_t += mapk(prediction_logits, categories, topk=mapk_topk)

            if (b + 1) % batch_iterations == 0 or (b + 1) == len(train_set_data_loader):
                optimizer.step()
                if loss_type == "center":
                    for param in criterion.center.parameters():
                        param.grad.data *= (1. / 0.5)
                    optimizer_centloss.step()

            sgdr_iterations += 1
            batch_count += 1
            epoch_batch_iter_count += 1

            optim_summary_writer.add_scalar("lr", get_learning_rate(optimizer), batch_count + 1)

        # TODO: recalculate epoch_iterations and maybe other values?
        train_data = train_data_provider.get_next()
        train_set.df = train_data.train_set_df
        val_set.df = train_data.val_set_df
        epoch_iterations = ceil(len(train_set) / batch_size)

        train_loss_avg = train_loss_sum_t.item() / epoch_batch_iter_count
        train_mapk_avg = train_mapk_sum_t.item() / epoch_batch_iter_count

        val_loss_avg, val_mapk_avg, val_accuracy_top1_avg, val_accuracy_top3_avg, val_accuracy_top5_avg, val_accuracy_top10_avg = \
            evaluate(model, val_set_data_loader, criterion, mapk_topk)

        if lr_scheduler_type == "reduce_on_plateau":
            lr_scheduler_plateau.step(val_mapk_avg)

        model_improved_within_sgdr_cycle = val_mapk_avg > sgdr_cycle_val_mapk_best_avg
        if model_improved_within_sgdr_cycle:
            torch.save(model.state_dict(), "{}/model-{}.pth".format(output_dir, ensemble_model_index))
            sgdr_cycle_val_mapk_best_avg = val_mapk_avg

        model_improved = val_mapk_avg > global_val_mapk_best_avg
        ckpt_saved = False
        if model_improved:
            torch.save(model.state_dict(), "{}/model.pth".format(output_dir))
            global_val_mapk_best_avg = val_mapk_avg
            epoch_of_last_improval = epoch
            ckpt_saved = True

        sgdr_reset = False
        if (epoch + 1 >= sgdr_next_cycle_end_epoch) and (epoch - epoch_of_last_improval >= sgdr_cycle_end_patience):
            sgdr_iterations = 0
            current_sgdr_cycle_epochs = int(current_sgdr_cycle_epochs * sgdr_cycle_epochs_mult)
            sgdr_next_cycle_end_epoch = epoch + 1 + current_sgdr_cycle_epochs + sgdr_cycle_end_prolongation

            ensemble_model_index += 1
            sgdr_cycle_val_mapk_best_avg = float("-inf")
            sgdr_cycle_count += 1
            sgdr_reset = True

            new_lr_min = lr_min * (lr_min_decay ** sgdr_cycle_count)
            new_lr_max = lr_max * (lr_max_decay ** sgdr_cycle_count)

            optimizer = create_optimizer(optimizer_type, model, new_lr_max)
            lr_scheduler = CosineAnnealingLR(optimizer, T_max=current_sgdr_cycle_epochs, eta_min=new_lr_min)
            if loss2_type is not None and sgdr_cycle_count >= loss2_start_sgdr_cycle:
                print("switching to loss type '{}'".format(loss2_type), flush=True)
                criterion = create_criterion(loss2_type, len(train_data.categories))

        optim_summary_writer.add_scalar("sgdr_cycle", sgdr_cycle_count, epoch + 1)

        train_summary_writer.add_scalar("loss", train_loss_avg, epoch + 1)
        train_summary_writer.add_scalar("mapk", train_mapk_avg, epoch + 1)
        val_summary_writer.add_scalar("loss", val_loss_avg, epoch + 1)
        val_summary_writer.add_scalar("mapk", val_mapk_avg, epoch + 1)

        epoch_end_time = time.time()
        epoch_duration_time = epoch_end_time - epoch_start_time

        print(
            "[%03d/%03d] %ds, lr: %.6f, loss: %.4f, val_loss: %.4f, acc: %.4f, val_acc: %.4f, ckpt: %d, rst: %d" % (
                epoch + 1,
                epochs_to_train,
                epoch_duration_time,
                get_learning_rate(optimizer),
                train_loss_avg,
                val_loss_avg,
                train_mapk_avg,
                val_mapk_avg,
                int(ckpt_saved),
                int(sgdr_reset)))

        print('{"chart": "best_val_mapk", "x": %d, "y": %.4f}' % (epoch + 1, global_val_mapk_best_avg))
        print('{"chart": "val_loss", "x": %d, "y": %.4f}' % (epoch + 1, val_loss_avg))
        print('{"chart": "val_mapk", "x": %d, "y": %.4f}' % (epoch + 1, val_mapk_avg))
        print('{"chart": "val_accuracy@1", "x": %d, "y": %.4f}' % (epoch + 1, val_accuracy_top1_avg))
        print('{"chart": "val_accuracy@3", "x": %d, "y": %.4f}' % (epoch + 1, val_accuracy_top3_avg))
        print('{"chart": "val_accuracy@5", "x": %d, "y": %.4f}' % (epoch + 1, val_accuracy_top5_avg))
        print('{"chart": "val_accuracy@10", "x": %d, "y": %.4f}' % (epoch + 1, val_accuracy_top10_avg))
        print('{"chart": "sgdr_cycle", "x": %d, "y": %d}' % (epoch + 1, sgdr_cycle_count))
        print('{"chart": "loss", "x": %d, "y": %.4f}' % (epoch + 1, train_loss_avg))
        print('{"chart": "mapk", "x": %d, "y": %.4f}' % (epoch + 1, train_mapk_avg))
        print('{"chart": "lr_scaled", "x": %d, "y": %.4f}' % (epoch + 1, 1000 * get_learning_rate(optimizer)))
        print('{"chart": "mem_used", "x": %d, "y": %.2f}' % (epoch + 1, psutil.virtual_memory().used / 2 ** 30))
        print('{"chart": "epoch_time", "x": %d, "y": %d}' % (epoch + 1, epoch_duration_time))

        sys.stdout.flush()

        if (sgdr_reset or lr_scheduler_type == "reduce_on_plateau") and epoch - epoch_of_last_improval >= patience:
            print("early abort due to lack of improval", flush=True)
            break

        if max_sgdr_cycles is not None and sgdr_cycle_count >= max_sgdr_cycles:
            print("early abort due to maximum number of sgdr cycles reached", flush=True)
            break

    optim_summary_writer.close()
    train_summary_writer.close()
    val_summary_writer.close()

    train_end_time = time.time()
    print()
    print("Train time: %s" % str(datetime.timedelta(seconds=train_end_time - train_start_time)), flush=True)

    if False:
        swa_model = create_model(type=model_type, input_size=image_size, num_classes=len(train_data.categories)).to(
            device)
        swa_update_count = 0
        for f in find_sorted_model_files(output_dir):
            print("merging model '{}' into swa model".format(f), flush=True)
            m = create_model(type=model_type, input_size=image_size, num_classes=len(train_data.categories)).to(device)
            m.load_state_dict(torch.load(f, map_location=device))
            swa_update_count += 1
            moving_average(swa_model, m, 1.0 / swa_update_count)
            # bn_update(train_set_data_loader, swa_model)
        torch.save(swa_model.state_dict(), "{}/swa_model.pth".format(output_dir))

    if confusion_set is not None:
        return

    test_data = TestData(input_dir)
    test_set = TestDataset(test_data.df, image_size, use_extended_stroke_channels)
    test_set_data_loader = \
        DataLoader(test_set, batch_size=batch_size, shuffle=False, num_workers=num_workers, pin_memory=pin_memory)

    model.load_state_dict(torch.load("{}/model.pth".format(output_dir), map_location=device))
    model = Ensemble([model])

    categories = train_data.categories

    submission_df = test_data.df.copy()
    predictions, predicted_words = predict(model, test_set_data_loader, categories, tta=False)
    submission_df["word"] = predicted_words
    np.save("{}/submission_predictions.npy".format(output_dir), np.array(predictions))
    submission_df.to_csv("{}/submission.csv".format(output_dir), columns=["word"])

    submission_df = test_data.df.copy()
    predictions, predicted_words = predict(model, test_set_data_loader, categories, tta=True)
    submission_df["word"] = predicted_words
    np.save("{}/submission_predictions_tta.npy".format(output_dir), np.array(predictions))
    submission_df.to_csv("{}/submission_tta.csv".format(output_dir), columns=["word"])

    val_set_data_loader = \
        DataLoader(val_set, batch_size=64, shuffle=False, num_workers=num_workers, pin_memory=pin_memory)

    model = load_ensemble_model(output_dir, 3, val_set_data_loader, criterion, model_type, image_size, len(categories))
    submission_df = test_data.df.copy()
    predictions, predicted_words = predict(model, test_set_data_loader, categories, tta=True)
    submission_df["word"] = predicted_words
    np.save("{}/submission_predictions_ensemble_tta.npy".format(output_dir), np.array(predictions))
    submission_df.to_csv("{}/submission_ensemble_tta.csv".format(output_dir), columns=["word"])

    confusion, _ = calculate_confusion(model, val_set_data_loader, len(categories))
    precisions = np.array([confusion[c, c] for c in range(confusion.shape[0])])
    percentiles = np.percentile(precisions, q=np.linspace(0, 100, 10))

    print()
    print("Category precision percentiles:")
    print(percentiles)

    print()
    print("Categories sorted by precision:")
    print(np.array(categories)[np.argsort(precisions)])


if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--input_dir", default="/storage/kaggle/quickdraw")
    argparser.add_argument("--output_dir", default="/artifacts")
    argparser.add_argument("--base_model_dir", default=None)
    argparser.add_argument("--image_size", default=64, type=int)
    argparser.add_argument("--augment", default=False, type=str2bool)
    argparser.add_argument("--use_dummy_image", default=False, type=str2bool)
    argparser.add_argument("--use_progressive_image_sizes", default=False, type=str2bool)
    argparser.add_argument("--progressive_image_size_min", default=32, type=int)
    argparser.add_argument("--progressive_image_size_step", default=16, type=int)
    argparser.add_argument("--progressive_image_epoch_step", default=7, type=int)
    argparser.add_argument("--epochs", default=500, type=int)
    argparser.add_argument("--batch_size", default=256, type=int)
    argparser.add_argument("--batch_iterations", default=1, type=int)
    argparser.add_argument("--test_size", default=0.1, type=float)
    argparser.add_argument("--fold", default=None, type=int)
    argparser.add_argument("--train_on_unrecognized", default=True, type=str2bool)
    argparser.add_argument("--confusion_set", default=None, type=int)
    argparser.add_argument("--num_category_shards", default=1, type=int)
    argparser.add_argument("--category_shard", default=0, type=int)
    argparser.add_argument("--eval_train_mapk", default=True, type=str2bool)
    argparser.add_argument("--mapk_topk", default=3, type=int)
    argparser.add_argument("--num_shard_preload", default=1, type=int)
    argparser.add_argument("--num_shard_loaders", default=1, type=int)
    argparser.add_argument("--num_workers", default=8, type=int)
    argparser.add_argument("--pin_memory", default=True, type=str2bool)
    argparser.add_argument("--lr_scheduler", default="cosine_annealing")
    argparser.add_argument("--lr_patience", default=3, type=int)
    argparser.add_argument("--lr_min", default=0.01, type=float)
    argparser.add_argument("--lr_max", default=0.1, type=float)
    argparser.add_argument("--lr_min_decay", default=1.0, type=float)
    argparser.add_argument("--lr_max_decay", default=1.0, type=float)
    argparser.add_argument("--model", default="cnn")
    argparser.add_argument("--patience", default=5, type=int)
    argparser.add_argument("--optimizer", default="sgd")
    argparser.add_argument("--loss", default="cce")
    argparser.add_argument("--loss2", default=None)
    argparser.add_argument("--loss2_start_sgdr_cycle", default=None, type=int)
    argparser.add_argument("--sgdr_cycle_epochs", default=5, type=int)
    argparser.add_argument("--sgdr_cycle_epochs_mult", default=1.0, type=float)
    argparser.add_argument("--sgdr_cycle_end_prolongation", default=0, type=int)
    argparser.add_argument("--sgdr_cycle_end_patience", default=2, type=int)
    argparser.add_argument("--max_sgdr_cycles", default=None, type=int)

    main()
