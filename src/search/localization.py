import math

import numpy as np

from src.util import normalize

# Exponent to use in approximate max pooling. 
# According to the paper, 10 is a good choice.
AML_EXP = 10.0

def _area_generator(shape, step_size, 
                    aspect_ratio=None, 
                    max_aspect_ratio_div=1.0):
    """A generator which returns areas of a rectangle whose aspect ratio 
    optionally does not exceed a given aspect ratio

    Args:
    shape: shape of the rectangle to sample areas from, 
        in the form (height, width)
    step_size: step size with which the areas are moved
    aspect_ratio (optional): aspect ratio of the considered areas, 
        computed as width / height
    max_aspect_ratio_div (optional): factor how much the aspect ratio of areas
        can be larger than the given aspect ratio

    Returns: A generator generating areas of the form (left, upper, 
        right, lower), where both sides are a multiple of the step size
    """
    height, width = shape
    if aspect_ratio:
        max_aspect_ratio_div = np.log(max_aspect_ratio_div)
    for x1 in range(0, width, step_size):
        for x2 in range(x1+step_size-1, width, step_size):
            for y1 in range(0, height, step_size):
                for y2 in range(y1+step_size-1, height, step_size):
                    if aspect_ratio:
                        # This calculation uses the fact that -log(a/x)=log(x/a)
                        # to ensure that a too large aspect ratio 
                        # is correctly skipped in both 'aspect ratio directions'
                        # e.g. for aspect_ratio=1, both area ratios 1:2 and 2:1
                        # are resulting in the same ratio which gets compared 
                        # to max_aspect_ratio_div
                        area_aspect_ratio = (x2-x1+1) / (y2-y1+1)
                        ratio = abs(np.log(aspect_ratio / area_aspect_ratio))
                        if ratio > max_aspect_ratio_div:
                            continue
                    yield (x1, y1, x2, y2)


def _compute_integral_image(image, exp=1):
    """Computes integral image.

    Optionally raises each entry to the power of exp before. 
    """
    image = image.astype(np.float64)
    image = np.power(image, exp)
    # Apply cumulative sum along both axis for integral image
    integral_image = np.cumsum(np.cumsum(image, axis=0), axis=1)
    return np.nan_to_num(integral_image)


def _integral_image_sum(integral_image, area):
    """Computes sum of area on an integral image

    Args:
    integral_image: integral image of shape (height, width, channels)
    area: Corner coordinates of area in the form of (left, upper, right, lower)

    Returns:
    Sum of the specified area of shape (channels,)
    """
    x1, y1, x2, y2 = area
    value = integral_image[y2, x2].copy()  # Whole area
    if x1 > 0: 
        value -= integral_image[y2, x1-1]  # Subtract left area
    if y1 > 0:
        value -= integral_image[y1-1, x2]  # Subtract top area
    if x1 > 0 and y1 > 0:
        value += integral_image[y1-1, x1-1]  # Add back top left area

    value = np.maximum(0.0, np.nan_to_num(value))

    if np.isscalar(value):
        return np.array([value])
    else:
        return value


def _compute_area_score(query, area, integral_image, exp=AML_EXP):
    """Computes cosine similarity between query representation and bounding box

    Args:
    query: L2 normalized representation of shape (1, dim)
    area: bounding box on integral image in the form of (left, upper, right, lower)
    integral_image: integral image of features on which the bounding box lies
    exp: constant used in approximate max pooling
    """
    max_pool = _integral_image_sum(integral_image, area) 
    max_pool = normalize(np.power(max_pool, 1.0 / exp))
    score = max_pool.dot(query.T)
    return float(np.clip(score, -1.0, 1.0))


def area_refinement(query, area, area_score, integral_image, 
                    iterations=10, max_step=3, exp=AML_EXP):
    """Improves bounding box by varying the box coordinates in an iterative 
    descent manner.

    Implements iterative bounding box refinement from arXiv:1511.05879v2. 
    Note that the description given in the paper is incomplete, and we follow 
    the C implementation Tolias et al. give in their code release here:
    https://gforge.inria.fr/frs/download.php/latestfile/5110/pkg_mac.tar.gz

    Args:
    query: L2 normalized representation of the object to find of shape (1, dim)
    area: bounding box to improve in the form of (left, upper, right, lower)
    area_score: score the bounding box to improve achieves
    integral_image: integral image of features on which the bounding box lies
    iterations: Number of times to run the improvement for each step_size
    max_step: box coordinates get varied from [1, max_step]
    exp: constant used in approximate max pooling
    
    Returns:
    Improved bounding box in the form of (left, upper, right, lower)
    """
    height, width, _ = integral_image.shape
    best_area = area
    best_score = area_score

    for step in range(max_step, 0, -1):
        for it in range(iterations):
            iter_best_area = best_area
            iter_best_score = best_score
            area = list(best_area)

            min_ranges = [0, 0, best_area[0], best_area[1]]
            max_ranges = [best_area[2], best_area[3], width-1, height-1]
            for coord in range(4):
                # Try decrease coordinate
                area[coord] = max(area[coord]-step, min_ranges[coord])
                score = _compute_area_score(query, area, integral_image, exp)
                if score > iter_best_score:
                    iter_best_score = score
                    iter_best_area = tuple(area)
                area[coord] = best_area[coord]

                # Try increase coordinate
                area[coord] = min(area[coord]+step, max_ranges[coord])
                score = _compute_area_score(query, area, integral_image, exp)
                if score > iter_best_score:
                    iter_best_score = score
                    iter_best_area = tuple(area)
                area[coord] = best_area[coord]

            best_area = iter_best_area
            best_score = iter_best_score
    return best_area


def localize(query, 
             features, 
             query_image_shape, 
             step_size=3, 
             aspect_ratio_factor=1.1):
    """Finds a bounding box for the query representation in the features

    Implements a rough localization algorithm via approximate 
    max-pooling localization (see arXiv:1511.05879v2).

    Args:
    query: L2 normalized representation of the object to find of shape (1, dim)
    features: convolutional feature map of the image to localize in, 
        of shape (height, width, dim)
    query_image_shape: shape of the original query image 
        in the form of (height, width)
    step_size, aspect_ratio_factor: area parameters

    Returns: bounding box on features fitting best to the query, in the form 
        of (left, upper, right, lower), and the score on of this bounding box
    """
    assert len(query_image_shape) == 2
    assert query.shape[-1] == features.shape[-1]    

    query_aspect_ratio = query_image_shape[1] / query_image_shape[0]

    integral_image = _compute_integral_image(features, AML_EXP)

    best_area = None
    best_score = -np.inf
    for area in _area_generator(integral_image.shape[:2], step_size, 
                                query_aspect_ratio, aspect_ratio_factor):
        score = _compute_area_score(query, area, integral_image, AML_EXP)
        if score > best_score:
            best_score = score
            best_area = area

    return best_area, best_score
