import sys
import os
from os import path
import shutil
import argparse
import csv
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from collections import defaultdict
import torch
from torchvision.transforms import functional as TF
from .. transforms import functional as NF
from .. logger import logger
from .. utils import save_image_snappy, save_image, ImageLoader, filename2key


def load_files_from_csv(txt):
    data = defaultdict(lambda: {})
    with open(txt, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            cols = list(row.values)
            src = cols.shift()
            key = filename2key(src, use_subdir=False)
            data[key] = {"src": src, "options": cols}
    return data


def load_files_from_dir(directory):
    files = ImageLoader.listdir(directory)
    data = defaultdict(lambda: {})
    for src in files:
        key = filename2key(src, use_subdir=False)
        data[key] = {"src": src, "options": []}
    return data


def crop_max(x, max_size):
    h, w = x.shape[1], x.shape[2]
    lh = min(h, max_size)
    lw = min(w, max_size)
    if lh != h or lw != w:
        return NF.crop_ref(x, 0, 0, lh, lw)
    else:
        return x


def _pair_crop_max(x, y, max_size):
    xh, xw = x.shape[1], x.shape[2]
    yh, yw = y.shape[1], y.shape[2]
    scale_h = xh / yh
    scale_w = xw / yw
    if abs(scale_w - scale_h) > 0.00001:
        raise ValueError(f"Unpredictable scale: x({xh},{xw}), y({yh},{yw})\n")
    assert(xh <= yh)
    lr_max_size = int(max_size * scale_h)
    lxh = min(xh, lr_max_size)
    lxw = min(xw, lr_max_size)
    if lxh != xh or lxw != xw:
        x = NF.crop_ref(x, 0, 0, lxh, lxw)
        lyh = int(lxh / scale_h)
        lyw = int(lxw / scale_w)
        y = NF.crop_ref(y, 0, 0, lyh, lyw)
        assert(lxh == int(lyh * scale_h) and lxw == int(lyw * scale_w))

    return x, y


def pair_crop_max(x, y, max_size):
    if x.shape[1] <= y.shape[1]:
        x, y = _pair_crop_max(x, y, max_size)
    else:
        y, x = _pair_crop_max(y, x, max_size)
    return x, y


def split_image(x, max_size, step_rate):
    h, w = x.shape[1], x.shape[2]
    images = []
    if h > max_size or w > max_size:
        crop_h = min(h, max_size)
        crop_w = min(w, max_size)
        step_h = int(crop_h * step_rate)
        step_w = int(crop_w * step_rate)
        for i in range(0, h - crop_h + 1, step_h):
            for j in range(0, w - crop_w + 1, step_w):
                images.append(NF.crop(x, i, j, crop_h, crop_w))
        return images
    else:
        return [x]


def _pair_split_image(x, y, max_size, split_step):
    xh, xw = x.shape[1], x.shape[2]
    yh, yw = y.shape[1], y.shape[2]
    scale_h = xh / yh
    scale_w = xw / yw
    if abs(scale_w - scale_h) > 0.00001:
        raise ValueError(f"Unpredictable scale: x({xh},{xw}), y({yh},{yw})\n")
    x_images = []
    y_images = []
    assert(xh <= yh)

    lr_max_size = int(max_size * scale_h)
    lxh = min(xh, lr_max_size)
    lxw = min(xw, lr_max_size)
    if xh != lxh or xw != lxw:
        crop_h = min(xh, lr_max_size)
        crop_w = min(xw, lr_max_size)
        step_h = int(crop_h * split_step)
        step_w = int(crop_w * split_step)
        for i in range(0, xh - crop_h + 1, step_h):
            for j in range(0, xw - crop_w + 1, step_w):
                assert(
                    i == int(int(i / scale_h) * scale_h) and
                    j == int(int(j / scale_w) * scale_w) and
                    crop_h == int(int(crop_h / scale_h) * scale_h) and
                    crop_w == int(int(crop_w / scale_w) * scale_w))  # avoid scaling issue
                x_images.append(NF.crop(x, i, j, crop_h, crop_w))
                y_images.append(NF.crop(y, int(i / scale_h), int(j / scale_w),
                                int(crop_h / scale_h), int(crop_w / scale_w)))

        return x_images, y_images
    else:
        return [x], [y]


def pair_split_image(x, y, max_size, split_step):
    if x.shape[1] <= y.shape[1]:
        x_images, y_images = _pair_split_image(x, y, max_size, split_step)
    else:
        y_images, x_images = _pair_split_image(y, x, max_size, split_step)
    return x_images, y_images


USE_SNAPPY_IMAGE = True #  False for debug
INPUT_DIR = "x"
TARGET_DIR = "y"


def save_image_task(im, output_path):
    if USE_SNAPPY_IMAGE:
        save_image_snappy(im, output_path)
    else:
        save_image(TF.to_pil_image(im), None, output_path)


def make_output_name(filename, no=-1):
    basename = filename2key(filename)
    ext = "sz" if USE_SNAPPY_IMAGE else "png"
    if no >= 0:
        return f"{basename}.{no:03d}.{ext}"
    else:
        return f"{basename}.{ext}"


def convert_data(data, has_x, args):
    input_dir = path.join(args.data_dir, INPUT_DIR)
    target_dir = path.join(args.data_dir, TARGET_DIR)
    os.makedirs(args.data_dir, exist_ok=True)
    if path.exists(input_dir):
        shutil.rmtree(input_dir)
    if path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    if has_x:
        os.makedirs(input_dir, exist_ok=True)

    y_files = [data[k]["y"]["src"] for k in sorted(data.keys())]
    y_loader = ImageLoader(files=y_files, max_queue_size=32)
    if has_x:
        x_files = [data[k]["x"]["src"] for k in sorted(data.keys())]
        x_loader = ImageLoader(files=x_files, max_queue_size=32)
    else:
        x_loader = [(None, None)] * len(y_files)

    with torch.no_grad(), PoolExecutor() as pool:
        # TODO: deadlock
        for (x_im, x_meta), (y_im, y_meta) in tqdm(zip(x_loader, y_loader), ncols=60):
            y = TF.to_tensor(y_im)
            if "alpha" in y_meta:
                sys.stdout.write(f"\nskip transparent png {y_meta['filename']}\n")
                continue
            x = None
            if x_im is not None:
                assert(filename2key(x_meta["filename"]) == filename2key(y_meta["filename"]))

                x = TF.to_tensor(x_im)
                if "alpha" in x_meta:
                    sys.stdout.write(f"\nskip transparent png {x_meta['filename']}\n")
                    continue
                if args.pad_x > 0:
                    pad_mode = 'reflect'
                    if args.zero_pad_x:
                        pad_mode = 'constant'
                    x = NF.pad(x, (args.pad_x,) * 4, mode=pad_mode)
                if args.pad_y > 0:
                    pad_mode = 'reflect'
                    if args.zero_pad_y:
                        pad_mode = 'constant'
                    y = NF.pad(y, (args.pad_y,) * 4, mode=pad_mode)
                if args.grayscale_x:
                    x = NF.to_grayscale(x)
                if args.grayscale_y:
                    y = NF.to_grayscale(y)

                if args.max_size > 0:
                    if args.split_image:
                        no = 0
                        for x, y in zip(*pair_split_image(x, y, args.max_size, args.split_step)):
                            pool.submit(save_image_task, x,
                                        path.join(input_dir, make_output_name(x_meta["filename"], no)))
                            pool.submit(save_image_task, y,
                                        path.join(target_dir, make_output_name(y_meta["filename"], no)))
                            no += 1
                    else:
                        x, y = pair_crop_max(x, y, args.max_size)
                        pool.submit(save_image_task, x,
                                    path.join(input_dir, make_output_name(x_meta["filename"])))
                        pool.submit(save_image_task, y,
                                    path.join(target_dir, make_output_name(y_meta["filename"])))
                else:
                    pool.submit(save_image_task, x,
                                path.join(input_dir, make_output_name(x_meta["filename"])))
                    pool.submit(save_image_task, y,
                                path.join(target_dir, make_output_name(y_meta["filename"])))
            else:
                if args.pad_y > 0:
                    pad_mode = 'reflect'
                    if args.zero_pad_y:
                        pad_mode = 'constant'
                    y = NF.pad(y, (args.pad_y,) * 4, mode=pad_mode)
                if args.grayscale_y:
                    y = NF.to_grayscale(y)

                if args.max_size > 0:
                    if args.split_image:
                        no = 0
                        for y in split_image(y, args.max_size, args.split_step):
                            pool.submit(save_image_task, y,
                                        path.join(target_dir, make_output_name(y_meta["filename"], no)))
                            no += 1
                    else:
                        y = crop_max(y, args.max_size)
                        pool.submit(save_image_task, y,
                                    path.join(target_dir, make_output_name(y_meta["filename"])))
                else:
                    pool.submit(save_image_task, y,
                                path.join(target_dir, make_output_name(y_meta["filename"])))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-x", type=str, help="input file or directory. optional.")
    parser.add_argument("--target", "-y", type=str, required=True, help="target file or directory")
    parser.add_argument("--data-dir", "-o", type=str, required=True, help="output directory")
    parser.add_argument("--max-size", type=int, default=0, help="max image size")
    parser.add_argument("--split-image", action="store_true", help="split image with --max-size")
    parser.add_argument("--split-step", type=float, default=0.5, help="step-size = --max-size * --split-step")
    parser.add_argument("--pad-x", type=int, default=0, help="padding size")
    parser.add_argument("--pad-y", type=int, default=0, help="padding size")
    parser.add_argument("--zero-pad-x", action="store_true", help="use zero padding")
    parser.add_argument("--zero-pad-y", action="store_true", help="use zero padding")
    parser.add_argument("--grayscale-x", action="store_true", default=False, help="convert to grayscale image")
    parser.add_argument("--grayscale-y", action="store_true", default=False, help="convert to grayscale image")

    args = parser.parse_args()
    logger.debug(str(args))

    if args.split_image and not args.max_size > 0:
        raise ValueError("--max-size is required for --split_image")
    if (args.pad_x > 0 or args.pad_y > 0) and args.max_size > 0:
        raise ValueError("--pad-(x|y) with --max-size is not supported")
    if args.input is None and (args.pad_x > 0 or args.zero_pad_x or args.grayscale_x):
        raise ValueError("--input is not specified but --pad-x,--zero-pad-x, --grayscale_x are specified")

    has_x = False
    if path.isdir(args.target):
        y_data = load_files_from_dir(args.target)
    elif path.splitext(args.target)[-1] in (".txt", ".csv"):
        y_data = load_files_from_csv(args.target)
    if args.input is not None:
        if path.isdir(args.input):
            x_data = load_files_from_dir(args.input)
        elif path.splitext(args.input)[-1] in (".txt", ".csv"):
            x_data = load_files_from_csv(args.input)

        # validate
        data = {}
        for y_key in y_data.keys():
            if y_key in x_data:
                data[y_key] = {"x": x_data[y_key], "y": y_data[y_key]}
            else:
                raise RuntimeError(f"{y_key} does not exist on --x side")
        has_x = True
    else:
        data = {}
        for k, v in y_data.items():
            data[k] = {"y": v}

    convert_data(data, has_x, args)

    return 0
