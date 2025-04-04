import pytorch_lightning as pl
import faiss
import skimage
from imageOps import *
from pytorch_lightning import seed_everything
import matplotlib.pyplot as plt
from einops import rearrange
import pickle
import numpy as np
import torch
from torch.utils.data import DataLoader, random_split, Dataset, ConcatDataset
from torchvision import datasets, transforms
import os
from osTools import *
from PIL import Image, ImageDraw, ImageFilter
import random
from segment_anything.utils.transforms import *
from segment_anything import SamPredictor, sam_model_registry, apply_transform_to_pil_without_sam_model, unnormalize_tensor
from torchTools import *
from args import *
import math
from shapely.affinity import affine_transform
from shapely.geometry import Point, Polygon
from shapely.ops import triangulate
from more_itertools import flatten
from logTools import *
import cv2
from copy import deepcopy
from boxes import *

def centroid(points):
    """Calculate the centroid of a polygon given its vertices."""
    x = [p[0] for p in points]
    y = [p[1] for p in points]
    return sum(x) / len(points), sum(y) / len(points)

def line_intersection(line1, line2):
    """Find the intersection of a line and a line segment."""
    p1, p2, p3, p4 = line1[0], line1[1], line2[0], line2[1]

    b = p1[0] - p3[0], p1[1] - p3[1]

    A = p4[0] - p3[0]
    B = -(p2[0] - p1[0])
    C = p4[1] - p3[1]
    D = -(p2[1] - p1[1])

    det = A * D - B * C

    # Parallel lines case
    if det == 0:
        return None

    u = (D * b[0] - B * b[1]) / det
    t = (-C * b[0] + A * b[1]) / det

    # Check if there is an intersection
    if t >= -1e-7 and -1e-7 <= u <= 1 + 1e-7:
        intersection = p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1])
        return intersection
    else:
        return None

def find_intersection(shape, point, centroid):
    """Find intersection of a ray from centroid to a point with the shape."""
    intersections = []
    for i in range(len(shape)):
        next_point = shape[(i + 1) % len(shape)]
        intersect = line_intersection((centroid, point), (shape[i], next_point))
        if intersect:
            intersections.append(intersect)
    assert len(intersections) > 0, f'No intersections found for shape, {shape}, point, {point} and centroid, {centroid}'
    return min(intersections, key=lambda x: np.linalg.norm(np.array(x) - np.array(centroid)))

def distance(point1, point2):
    """Calculate the Euclidean distance between two points."""
    return torch.sqrt((point1[0] - point2[0])**2 + (point1[1] - point2[1])**2)

def tapering_function(pointA, pointC, pointB):
    """Tapering function that is 1 at C and tapers off towards B."""
    AC = distance(pointA, pointC)
    AB = distance(pointA, pointB)
    if AB == 0: return torch.tensor(0.0)
    ratio = AC / AB
    return torch.sigmoid(-10 * (ratio - 0.5))

def find_confidence_score (shape, point) : 
    center = centroid(shape)
    inters = find_intersection(shape, point, center)
    return tapering_function(point, center, inters)

def create_shape_mask (points_np, box, chosen_width) : 
    """
    Creates a tight mask for the shape with img_width
    """ 
    points_np[:, 0] -= box.x
    points_np[:, 1] -= box.y
    points_np *= chosen_width / box.w
    points_np = np.clip(points_np, 0, np.inf)
    points_np = points_np.astype(int)
    new_box = points_to_box(points_np)
    mask = np.zeros((new_box.h, new_box.w)).astype(np.uint8)
    cv2.fillPoly(mask, [points_np], 255)
    return mask

def alpha_composite_img_in_box (img_a, img_b, box) : 
    img_b = img_b.resize((box.w, box.h)) 
    img_b_big = Image.new('RGBA', img_a.size, (0, 0, 0, 0))
    img_b_big.paste(img_b, (box.x, box.y)) 
    result = Image.alpha_composite(img_a.convert('RGBA'), img_b_big).convert('RGB')
    return result

def get_random_crop_from_image (img, points) :
    # first figure out an appropriate scaling factor
    points_np = np.array(points).astype(float)
    shape_box = points_to_box(points_np) 
    aspect = shape_box.h / shape_box.w 
    img_w, img_h = img.size
    pad_w, pad_h = int(img_w * 0.075), int(img_h * 0.075)
    chosen_width = int(random.choice([0.5, 0.6, 0.8, 0.9]) * min(img_w - 2 * pad_w, (img_h - 2 * pad_h) / aspect))
    # create shape mask 
    shape_mask = create_shape_mask(points_np, shape_box, chosen_width) 
    H, W = shape_mask.shape
    # create a random crop with the shape mask
    st_x, st_y = random.randint(pad_w, img_w - W - pad_w - 1), random.randint(0, img_h - H - pad_h - 1)
    img_np = np.array(img)
    rgb = img_np[st_y:st_y + H, st_x:st_x + W] 
    rgba = np.concatenate((rgb, shape_mask.reshape(H, W, 1)), axis=2)
    return Image.fromarray(rgba, 'RGBA')

def prepare_rand_comic_panel (base_img, imgs, shapes) : 
    for img, shape in zip(imgs, shapes) : 
        crop = get_random_crop_from_image(img, shape)
        base_img = alpha_composite_img_in_box(base_img, crop, points_to_box(np.array(shape)))
    return base_img

def config_plot(ax):
    """ Function to remove axis tickers and box around a given axis """
    ax.set_frame_on(False)
    ax.axis('off')

def polygon_area (shape) : 
    """ calculate the area of a polygon using shapely """ 
    if isinstance(shape, Polygon) :
        polygon = shape
    else : 
        polygon = Polygon(shape)
    return sum(t.area for t in triangulate(polygon))

""" Stolen from https://codereview.stackexchange.com/questions/69833/generate-sample-coordinates-inside-a-polygon """
def sample_random_points_in_polygon(shape, k=1):
    "Return list of k points chosen uniformly at random inside the polygon."
    polygon = Polygon(shape)
    areas = []
    transforms = []
    for t in triangulate(polygon):
        areas.append(t.area)
        (x0, y0), (x1, y1), (x2, y2), _ = t.exterior.coords
        transforms.append([x1 - x0, x2 - x0, y2 - y0, y1 - y0, x0, y0])
    points = []
    for transform in random.choices(transforms, weights=areas, k=k):
        x, y = [random.random() for _ in range(2)]
        if x + y > 1:
            p = Point(1 - x, 1 - y)
        else:
            p = Point(x, y)
        points.append(affine_transform(p, transform))
    return [p.coords for p in points]

def sorted_points(points):
    points_by_x = sorted(points, key=lambda p: p[1])
    first_point, second_point = sorted(points_by_x[:2], key=lambda p: p[0])
    third_point, fourth_point = list(reversed(sorted(points_by_x[2:], key=lambda p: p[0])))
    return [first_point, second_point, third_point, fourth_point]

def merge_boxes(box_list) :
    np_box_list = np.array(box_list).astype(int)
    x, X, y, Y = np_box_list[:, 0].min(), np_box_list[:, 1].max(), np_box_list[:, 2].min(), np_box_list[:, 3].max()
    return x, X, y, Y

def composite_mask(image, mask, alpha=0.2):
    image = skimage.transform.resize(image, mask.shape, preserve_range=True).astype(np.uint8)
    white = [255, 255, 255]
    mask_rgb = np.zeros_like(image)
    mask_rgb[mask == 1] = white
    composite = np.uint8(image * (1 - alpha) + mask_rgb * alpha)
    return composite

def visualize_batch (sam_model, batch, dataset, outputs=None, save_to=None) : 
    """ This visualized a batch from the dataset """ 
    batch = tensorApply(batch, lambda x: x.to(torch.device('cuda')))
    # extract stuff from batch

    if 'features' in batch : 
        features = batch['features']
    else : 
        assert 'img' in batch, "Either image or features needed for this" 
        with torch.no_grad() : 
            features = sam_model.image_encoder(batch['img'])

    point_coords = batch['point_coords']
    point_labels = batch['point_labels']
    original_size = batch['original_size']
    input_size = batch['input_size']
    shape = batch['shape']
    index = batch['index']

    if outputs is None:
        points = (point_coords, point_labels)

        sparse_embeddings, dense_embeddings = sam_model.prompt_encoder(
            points=points,
            boxes=None,
            masks=None,
        )
        
        # Predict masks
        low_res_masks, iou_predictions = sam_model.mask_decoder(
            image_embeddings=features,
            image_pe=sam_model.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse_embeddings,
            dense_prompt_embeddings=dense_embeddings,
            multimask_output=True,
            interleave=False, # this ensures correct behaviour when each prompt is for a different image
        )
    else :
        low_res_masks, iou_predictions = outputs['low_res_masks'], outputs['iou_predictions']

    # Upscale the masks to the original image resolution
    masks = sam_model.postprocess_masks_size_list(low_res_masks, input_size, original_size)
    n_masks = len(masks) 
    best_masks = []
    # process and select best masks
    for i, mask in enumerate(masks) : 
        mask_threshed = (mask > sam_model.mask_threshold).squeeze()
        best_masks.append(mask_threshed[torch.argmax(iou_predictions[i])].detach().cpu().numpy()) 

    plots = []
    for i in range(n_masks) :
        fig, ax = plt.subplots(1, 1)
        mask_to_show = best_masks[i]

        if outputs is not None: 
            # Plot predictions
            pred_pts = normalized_point_to_image_point(outputs['pred'][i], input_size[i], original_size[i]).detach().cpu().numpy()
            ax.scatter(pred_pts[:, 0], pred_pts[:, 1], c=[(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)], marker='x', alpha=0.5)

        # Plot GT points
        pts = normalized_point_to_image_point(shape[i], input_size[i], original_size[i]).detach().cpu().numpy()
        ax.scatter(pts[:, 0], pts[:, 1], c=[(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)], alpha=0.5)

        # Plot Query Point
        sample_point = model_point_to_image_point(point_coords[i], input_size[i], original_size[i]).detach().cpu().numpy()
        ax.scatter(sample_point[:, 0], sample_point[:, 1], c='g') 

        # handle the case where the image is provided in the batch
        if 'img' in batch : 
            h, w = input_size[i]
            img = unnormalize_tensor(batch['img'][i])
            vis_img = (255. * normalize2UnitRange(img).permute(1,2,0).detach().cpu().numpy()[:h, :w]).astype(np.uint8) 
        else : 
            vis_img = np.array(Image.open(f'{dataset.folders[index[i]]}/img.png'))

        vis_img = composite_mask(vis_img, mask_to_show.astype(np.uint8))
        ax.imshow(vis_img)

        # remove ticks and box
        config_plot(ax)

        plots.append(fig_to_pil(fig))
        plt.close(fig)

    if save_to is not None: 
        make_image_grid(plots, False).save(save_to)
    else :
        plt.imshow(make_image_grid(plots, False))
        plt.show()

def visualize_batch_without_sam (batch, dataset, outputs=None, save_to=None) : 
    """ This visualized a batch from the dataset """ 
    point_coords = batch['point_coords']
    point_labels = batch['point_labels']
    original_size = batch['original_size']
    input_size = batch['input_size']
    shape = batch['shape']
    index = batch['index']

    plots = []
    for i in range(shape.shape[0]) :
        fig, ax = plt.subplots(1, 1)

        # Plot GT points
        pts = normalized_point_to_image_point(shape[i], input_size[i], original_size[i]).detach().cpu().numpy()
        # ax.scatter(pts[:, 0], pts[:, 1], c=[(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)], alpha=0.5)

        # Plot Query Point
        sample_point = model_point_to_image_point(point_coords[i], input_size[i], original_size[i]).detach().cpu().numpy()
        # ax.scatter(sample_point[:, 0], sample_point[:, 1], c='g') 

        # handle the case where the image is provided in the batch
        if 'img' in batch : 
            h, w = input_size[i]
            img = unnormalize_tensor(batch['img'][i])
            vis_img = (255. * normalize2UnitRange(img).permute(1,2,0).detach().cpu().numpy()[:h, :w]).astype(np.uint8) 
        else : 
            vis_img = np.array(Image.open(f'{dataset.folders[index[i]]}/img.png'))

        ax.imshow(vis_img)

        # remove ticks and box
        config_plot(ax)

        plots.append(fig_to_pil(fig))
        plt.close(fig)

    if save_to is not None: 
        make_image_grid(plots, False).save(save_to)
    else :
        plt.imshow(make_image_grid(plots, False))
        plt.show()

def fig_to_pil (fig) : 
    fig.canvas.draw()
    data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
    data = data.reshape(fig.canvas.get_width_height()[::-1] + (3,))
    return Image.fromarray(data)

def box_to_shape(box) : 
    x, X, y, Y = box
    return sorted_points([(x, y), (x, Y), (X, Y), (X, y)])

def shape_to_box(shape) : 
    np_shape = np.array(shape).astype(int) 
    x, X, y, Y = np_shape[:, 0].min(), np_shape[:, 0].max(), np_shape[:, 1].min(), np_shape[:, 1].max()
    return x, X, y, Y

def normalized_point_to_image_point (pt, input_size, original_size) : 
    target_img_size = int(max(input_size))
    factor = max(original_size) / max(input_size)
    pt = factor * target_img_size * ((pt / 2.0) + 0.5)
    return pt

def model_point_to_image_point(pt, input_size, original_size) : 
    factor = max(original_size) / max(input_size)
    pt = factor * pt
    return pt

def original_size_to_input_size(transform, original_size): 
    """ convert original size to the size seen by the model """
    input_size_np = transform.apply_coords(np.array((original_size,)), original_size)
    input_size_rounded = [round(x) for x in input_size_np.tolist()[0]]
    return tuple(input_size_rounded)

def correct_box (box) : 
    """ X should be along image width and Y should be along image height """
    x, X, y, Y = box
    return (y, Y, x, X)

def correct_point (point) : 
    """ X should be along image width and Y should be along image height """
    y, x = point
    return x, y

def fix_boxes (boxes) : 
    boxes = [correct_box(_) for _ in boxes]
    return boxes

def sample_random_point_in_box (box) : 
    x, X, y, Y = box
    return (random.randint(x, X), random.randint(y, Y))

def fix_points (shapes) : 
    shapes = [sorted([correct_point(_) for _ in shape]) for shape in shapes]
    return shapes

def deterministic_shuffle(lst, seed=0):
    random.seed(seed)
    random.shuffle(lst)
    return lst

def list_base_dir(base_dir):
    """ lists one dir down in a directory """
    result = []
    for root, dirs, _ in os.walk(base_dir):
        for d in dirs:
            path = os.path.join(root, d)
            subfolders = [os.path.join(path, sub) for sub in os.listdir(path)]
            result.extend(subfolders)
        break
    return result

def split_train_test (data, train_percent) : 
    train_size = int(len(data) * train_percent)
    train_data = data[:train_size]
    test_data = data[train_size:]
    return train_data, test_data

class FrameDataset(Dataset):

    def __init__(self, folders_list, target_img_size=1024, precompute_features=True):
        self.folders = folders_list
        self.target_img_size = target_img_size
        self.precompute_features = precompute_features
        if self.precompute_features : 
            self.features = torch.cat([torch.load(osp.join(_, 'vit_h_features.pt'), map_location='cpu') for _ in self.folders])
        self.pil_paths = [osp.join(_, 'img.png') for _ in self.folders]
        self.img_sizes = [np.array(Image.open(osp.join(_, 'img.png'))).shape[:2] for _ in self.folders]
        self.transform = ResizeLongestSide(target_img_size)
        # now load the data
        self.data = []
        for base_path in self.folders: 
            with open(osp.join(base_path, 'data.pkl'), 'rb') as fp :
                self.data.append(pickle.load(fp))
        # fix boxes and shapes
        for i in range(len(self.data)) :
            # TODO: Visualize whether box and shapes are identical
            self.data[i]['boxes'] = fix_boxes(self.data[i]['boxes'])
            self.data[i]['shapes'] = [box_to_shape(_) for _ in self.data[i]['boxes']] # fix_points(self.data[i]['shapes'])

    def __len__(self):
        return len(self.folders)

    def __getitem__(self, i):
        # get features
        if self.precompute_features : 
            features = self.features[i] 
        else : 
            img = apply_transform_to_pil_without_sam_model(Image.open(self.pil_paths[i]), 'cpu').squeeze()

        # get original and transformed image sizes
        original_size = self.img_sizes[i]
        input_size = original_size_to_input_size(self.transform, original_size)

        # pick a random shape id
        N = len(self.data[i]['shapes'])
        shape_id = random.randint(0, N - 1)

        # prepare the shape
        shape = self.data[i]['shapes'][shape_id]
        shape = torch.from_numpy(self.transform.apply_coords(np.array(shape).astype(np.float32), original_size))

        # now sample random points from the corresponding box
        point_coords = [sample_random_point_in_box(self.data[i]['boxes'][shape_id])] 
        point_coords = torch.from_numpy(self.transform.apply_coords(np.array(point_coords).astype(np.float32), original_size)) # [1, 2]

        point_confidence_score = find_confidence_score(shape, point_coords[0]).float().unsqueeze(0) # [1]

        # normalize the shape
        shape = (2.0 * (shape / self.target_img_size) - 1.0).float()

        # all the query points are foreground in our case
        point_labels = torch.ones((1,)).float()

        # cast to tensor 
        original_size = torch.tensor(original_size)
        input_size = torch.tensor(input_size)

        if self.precompute_features : 
            return dict(
                features=features,                               # [256, 64, 64]
                point_coords=point_coords,                       # [1, 2] 
                point_labels=point_labels,                       # [1]
                original_size=original_size,                     # [2]
                input_size=input_size,                           # [2]
                shape=shape,                                     # [4, 2], float, [-1.0, 1.0]
                index=torch.tensor([i]),                         # [1]
                point_confidence_score=point_confidence_score    # [1]
            )
        else : 
            # now the model training code will compute features. We'll just give the image
            return dict(
                img=img,                                         # [3, 1024, 1024]
                point_coords=point_coords,                       # [1, 2] 
                point_labels=point_labels,                       # [1]
                original_size=original_size,                     # [2]
                input_size=input_size,                           # [2]
                shape=shape,                                     # [4, 2], float, [-1.0, 1.0]
                index=torch.tensor([i]),                         # [1]
                point_confidence_score=point_confidence_score    # [1]
            )

def transpose_points (pts) : 
    assert is_iterable(pts), '(transpose_points): I need an iterable'
    if all(isinstance(_, int) for _ in pts) or all(isinstance(_, np.int64) for _ in pts) : 
        assert len(pts) == 2, f'(transpose_points): I don\'t know what to do with {len(pts)}-D point' 
        x, y = pts
        return y, x
    else : 
        return [transpose_points(_) for _ in pts]

def transpose_simple_comic_layout_data (data) : 
    return {
        'img': data['img'].rotate(90, expand=True).transpose(Image.FLIP_TOP_BOTTOM),
        'original_size': transpose_points(data['original_size']),
        'shapes': [sorted_points(_) for _ in transpose_points(data['shapes'])]
        # NOTE: ^ this function is wrongly named in this context. 
        # There is nothing wrong with these boxes. The aim is to 
        # simply transpose (switch x and y coordinates).
    }

def convert_box_pair_to_slanted_shapes(box1, box2):
    x1, X1, y1, Y1 = box1
    x2, X2, y2, Y2 = box2

    # Delta shift factor
    d_top = int(random.uniform(-0.4, 0.4) * min(X1 - x1, X2 - x2))
    d_bot = int(random.uniform(-0.4, 0.4) * min(X1 - x1, X2 - x2))

    # For shape 1
    shape_1 = sorted_points([(x1, y1), (X1 + d_top, y1), (X1 + d_bot, Y1), (x1, Y1)])

    # For shape 2
    shape_2 = sorted_points([(x2 + d_top, y2), (X2, y2), (X2, Y2), (x2 + d_bot, Y2)])

    return shape_1, shape_2

def generate_simple_comic_layout(image_index=None, image_paths=None):
    if image_index is not None and image_paths is not None :
        # make image index so that we can mine similar images
        # to make our dummy comic book panel
        with open(image_paths) as fp : 
            image_paths = [_.strip() for _ in fp.readlines()]
        image_index = np.load(image_index).astype(np.float32)
        image_index = image_index / np.linalg.norm(image_index, axis=1).reshape(-1, 1)
        faiss_index = faiss.IndexFlatIP(768)
        faiss_index.add(image_index)
        print('Finished preparing Image Index') 

    while True :
        # Choose an aspect ratio
        aspect_ratios = [
            {"width": 1, "height": 1},
            {"width": 4, "height": 3},
            {"width": 16, "height": 9},
            {"width": 21, "height": 9},
            {"width": 3, "height": 2},
            {"width": 9, "height": 16},
            {"width": 2.35, "height": 1},
            {"width": 1.85, "height": 1},
            {"height": 4, "width": 3},
            {"height": 16, "width": 9},
            {"height": 21, "width": 9},
            {"height": 3, "width": 2},
            {"height": 9, "width": 16},
            {"height": 2.35, "width": 1},
            {"height": 1.85, "width": 1}
        ]
        chosen_ratio = random.choice(aspect_ratios)

        # Set image dimensions
        if chosen_ratio["width"] > chosen_ratio["height"]:
            width = 1024
            height = int(width / chosen_ratio["width"] * chosen_ratio["height"])
        else:
            height = 1024
            width = int(height * chosen_ratio["width"] / chosen_ratio["height"])

        # Create an image with background color
        background_color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        # Most of the times the panels are black or white. Reflect that
        background_color = random.choice([(255, 255, 255), (0, 0, 0), background_color])

        img = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(img)

        # Border settings
        border_thickness = random.randint(1, 20)
        border_color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))

        # Whether to fill the box
        box_fill = None if random.choice([True, False]) else (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))

        # Whether to draw rectangle or rounded rectangle
        draw_rect = random.random() > 0.25
        rect_radius = random.randint(1, 50)

        # Gutter settings
        gutter = random.choice([True, True, True, False])
        gutter_width = random.randint(1, 50) if gutter else 0

        # Margin settings
        margin_x = random.randint(0, width // 10)
        margin_y = random.randint(0, height // 10)

        # Rows and Columns
        rows = random.choice([1, 2, 3, 4])
        row_height = (height - 2 * margin_y - (rows - 1) * gutter_width) // rows

        y_start = margin_y
        boxes = []
        for _ in range(rows):
            boxes.append([])
            cols = random.choice([1, 2, 3, 4])
            col_width = (width - 2 * margin_x - (cols - 1) * gutter_width) // cols

            x_start = margin_x
            for _ in range(cols):
                boxes[-1].append((x_start, x_start + col_width, y_start, y_start + row_height))
                x_start += col_width + gutter_width

            y_start += row_height + gutter_width

        shapes = [] 
        shapeTypes = [] # True if a box. False if general quad
        for i in range(rows) :  
            j = 0
            cols = len(boxes[i])
            while j < cols : 
                merge_forward = (random.random() < 0.1)
                if merge_forward : 
                    nj = random.choice(list(range(j + 1, cols + 1)))
                    shapes.append(box_to_shape(merge_boxes(boxes[i][j:nj])))
                    shapeTypes.append(True) 
                    j = nj
                else : 
                    make_slant = (random.random() < 1.0) and j + 1 < cols 
                    if make_slant : 
                        shape_1, shape_2 = convert_box_pair_to_slanted_shapes(boxes[i][j], boxes[i][j+1])
                        shapes.extend([shape_1, shape_2])
                        shapeTypes.extend([False, False])
                        j = j + 2
                    else : 
                        shapes.append(box_to_shape(boxes[i][j]))
                        shapeTypes.append(True)
                        j = j + 1

        # Render the frames on the image
        for shape, shapeType in zip(shapes, shapeTypes) : 
            if shapeType : 
                # With very low probability, don't draw this shape
                # Used to simulate negative space frames
                dont_draw = random.random() < 0.005
                if dont_draw : 
                    continue
                # shape is a box
                x, X, y, Y =  shape_to_box(shape)
                if draw_rect : 
                    draw.rectangle(
                        [(x, y), (X, Y)], 
                        fill=box_fill, 
                        outline=border_color, 
                        width=border_thickness
                    )
                else : 
                    draw.rounded_rectangle(
                        [(x, y), (X, Y)], 
                        radius=rect_radius, 
                        fill=box_fill, 
                        outline=border_color, 
                        width=border_thickness
                    )
            else : 
                # shape is a general quad
                draw.polygon(
                    shape, 
                    fill=box_fill, 
                    outline=border_color,
                    width=border_thickness
                )

        # Fill the shapes with images
        if image_index is not None and image_paths is not None :
            add_images = random.random() > 0.166
            # if there is no gutter, then it is a bit hard to make out what is happening
            if add_images and gutter: 
                first_image_id = random.randint(0, len(image_paths) - 1) 
                all_image_idx = faiss_index.search(image_index[first_image_id:first_image_id+1], len(shapes))[1][0].tolist()
                imgs = [Image.open(image_paths[_]).convert('RGB') for _ in all_image_idx]
                try : 
                    img_ = deepcopy(img)
                    img_ = prepare_rand_comic_panel(img_, imgs, shapes)
                    img = img_
                except Exception as e: 
                    print(e)

        # Apply gaussian blur so that not overly dependent on sharp edges
        apply_gaussian_blur = random.random() > 0.25
        kernel_size = random.choice([2,3,4,5]) 
        if apply_gaussian_blur : 
            img = img.filter(ImageFilter.GaussianBlur(kernel_size))

        data = {
            'img': img, 
            'original_size': tuple(reversed(img.size)),
            'shapes': shapes
        }

        # Randomly transpose rows and columns for added flair
        transpose_data = random.choice([True, False])
        if transpose_data : 
            data = transpose_simple_comic_layout_data(data) 

        yield data

class RandomComicLayoutDataset (Dataset) : 

    def __init__(self, random_gen_len=10000, target_img_size=1024, image_index=None, image_paths=None):
        self.random_gen_len = random_gen_len
        self.target_img_size = target_img_size
        self.transform = ResizeLongestSide(target_img_size)
        self.generator = generate_simple_comic_layout(image_index=image_index, image_paths=image_paths)

    def __len__(self):
        return self.random_gen_len # len(self.folders)

    def __getitem__(self, i):
        # get features
        data = next(self.generator)
        img = apply_transform_to_pil_without_sam_model(data['img'], 'cpu').squeeze()

        # get original and transformed image sizes
        original_size = data['original_size'] 
        input_size = original_size_to_input_size(self.transform, original_size)

        shapes = data['shapes']
        N = len(shapes)
        shape_id = random.randint(0, N - 1)

        # prepare the shape
        shape = shapes[shape_id]

        # now sample random points from the corresponding shape
        point_coords = sample_random_points_in_polygon(shape, 1)[0]

        # assign a confidence score ot the sampled point on the basis of closeness to centre of shape
        point_coords = torch.from_numpy(self.transform.apply_coords(np.array(point_coords).astype(np.float32), original_size)) # [1, 2]
        shape = torch.from_numpy(self.transform.apply_coords(np.array(shape).astype(np.float32), original_size))
        point_confidence_score = find_confidence_score(shape, point_coords[0]).float().unsqueeze(0) # [1]

        # normalize the shape
        shape = (2.0 * (shape / self.target_img_size) - 1.0).float()

        # all the query points are foreground in our case
        point_labels = torch.ones((1,)).float()

        # cast to tensor 
        original_size = torch.tensor(original_size)
        input_size = torch.tensor(input_size)

        # now the model training code will compute features. We'll just give the image
        return dict(
            img=img,                                         # [3, 1024, 1024]
            point_coords=point_coords,                       # [1, 2] 
            point_labels=point_labels,                       # [1]
            original_size=original_size,                     # [2]
            input_size=input_size,                           # [2]
            shape=shape,                                     # [4, 2], float, [-1.0, 1.0]
            index=torch.tensor([i]),                         # [1]
            point_confidence_score=point_confidence_score    # [1]
        )

class FrameDataModule(pl.LightningDataModule):

    def __init__(self, args):
        super().__init__()
        print('datamodule_poly.py data module')
        self.image_index = args.image_index
        self.image_paths = args.image_paths
        self.base_dir = args.base_dir
        self.num_workers = args.num_workers
        self.batch_size = args.batch_size
        self.precompute_features = args.precompute_features
        self.files = deterministic_shuffle(list_base_dir(self.base_dir))
        self.train_files, self.test_files = split_train_test(self.files, 0.9)

    def setup(self, stage=None):
        if self.precompute_features : 
            self.train_data = FrameDataset(self.train_files, precompute_features=self.precompute_features) 
            self.test_data = FrameDataset(self.test_files, precompute_features=self.precompute_features)
        else : 
            print('Using two datasets') 
            self.train_data = ConcatDataset([
                FrameDataset(self.train_files, precompute_features=self.precompute_features),
                RandomComicLayoutDataset(image_index=self.image_index, image_paths=self.image_paths) 
            ])
            self.test_data = ConcatDataset([
                FrameDataset(self.train_files, precompute_features=self.precompute_features),
                RandomComicLayoutDataset(random_gen_len=100, image_index=self.image_index, image_paths=self.image_paths) 
            ])

    def train_dataloader(self):
        return DataLoader(self.train_data, batch_size=self.batch_size, num_workers=self.num_workers, shuffle=True)

    def val_dataloader(self):
        return DataLoader(self.test_data, batch_size=self.batch_size, num_workers=self.num_workers)

    def test_dataloader(self):
        return DataLoader(self.test_data, batch_size=self.batch_size, num_workers=self.num_workers)

if __name__ == "__main__" : 
    seed = 2
    # sam_model = sam_model_registry["vit_h"](checkpoint="./checkpoints/sam_vit_h_4b8939.pth").cuda()
    # test with precomputed_features=False
    seed_everything(seed)
    datamodule = FrameDataModule(DictWrapper(dict(
        base_dir='../comic_data', 
        batch_size=4, 
        num_workers=0, 
        precompute_features=False,
        image_index='../danbooru2021/clip_l_14_all.npy', 
        image_paths='../danbooru2021/clip_l_14_all.txt'
    )))
    datamodule.setup()
    for idx, batch in enumerate(datamodule.train_dataloader()) : 
        print(batch.keys())
        for k in batch.keys() :
            print(k, batch[k].shape)
        visualize_batch_without_sam(batch, datamodule.train_data, save_to=f'img_{idx}.png')
        if idx > 30 :
            break
