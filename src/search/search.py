import os
import sys
import argparse

import numpy as np

from src.util import crop_image, convert_image
from src.features import (compute_features, compute_representation, 
                          compute_localization_representation)
from src.search.search_model import SearchModel
from src.search.localization import localize, area_refinement


def _descending_argsort(array, k):
    """Return indices that index the highest k values in an array"""
    indices = np.argpartition(array, -k)[-k:]
    indices = indices[np.argsort(array[indices])]  # Sort indices
    return indices[::-1]


def _ascending_argsort(array, k):
    """Return indices that index the lowest k values in an array"""
    indices = np.argpartition(array, k-1)[:k]
    indices = indices[np.argsort(array[indices])]  # Sort indices
    return indices


def query(query_features, feature_store, top_n=0):
    """Query stored features for similarity against a passed feature

    Args:
    query_features: Feature to query for, shape (1, dim)
    feature_store: 2D-array of n features to compare to, shape (n, dim)
    top_n:  if zero, return all results in descending order
            if positive, return only the best top_n results
            if negative, return only the worst top_n results

    Returns: (indices, similarities), where indices is an array of top_n 
        indices of entries in the feature_store sorted by decreasing 
        similarity, and similarities contains the corresponding 
        similarity value for each entry.
    """
    # Cosine similarity measure
    similarity = feature_store.dot(query_features.T).flatten()

    k = min(len(feature_store), abs(top_n))
    if top_n >= 0:  # Best top_n features
        indices = _descending_argsort(similarity, k)
    else:  # Worst top_n features
        indices = _ascending_argsort(similarity, k)
    
    return indices, similarity[indices]


def search_roi(search_model, image, roi=None, top_n=0, verbose=True):
    """Query the feature store for a region of interest on an image

    Args:
    search_model: instance of the SearchModel class
    image: RGB PIL image to take roi of
    roi: bounding box in the form of (left, upper, right, lower)
    top_n: how many query results to return. top_n=0 returns all results

    Returns: (indices, similarities, bounding_boxes), where indices is an 
        array of top_n indices of entries in the feature_store sorted 
        by decreasing similarity, similarities contains the 
        corresponding similarity score for each entry, and bounding boxes 
        is a list of tuples of the form (left, upper, right, lower) 
        specifying the rough location of the found objects.
    """
    assert top_n >= 0
    avg_query_exp_n = 5  # How many top entries to use in query expansion

    crop = convert_image(crop_image(image, roi))
    crop = search_model.preprocess_fn(crop)

    query_features = compute_features(search_model.model, crop)
    query_repr = compute_representation(query_features, search_model.pca)
    localization_repr = compute_localization_representation(query_features)

    scale_x = crop.shape[2] / query_features.shape[1]
    scale_y = crop.shape[1] / query_features.shape[0]

    if verbose:
        from timeit import default_timer as timer
        start = timer()

    # Step 1: initial retrieval
    indices, sims = query(query_repr, search_model.feature_store, top_n)

    if verbose:
        end = timer()
        print('Retrieval took {:.6f} seconds'.format(end-start))
        start = timer()

    # Step 2: localization and re-ranking
    feature_shapes = {}
    bounding_boxes = np.empty(len(indices), dtype=(int, 4))
    bounding_box_reprs = np.empty((len(indices), query_repr.shape[-1]))
    
    for idx, feature_idx in enumerate(indices):
        features = search_model.get_features(feature_idx)
        feature_shapes[feature_idx] = features.shape

        bbox, score = localize(localization_repr, features, crop.shape[1:3])
        bounding_boxes[idx] = area_refinement(query, best_area, best_score, 
                                              integral_image)

        x1, y1, x2, y2 = bounding_boxes[idx]
        bbox_repr = compute_representation(features[y1:y2+1, x1:x2+1])
        bounding_box_reprs[idx] = bbox_repr
        
    reranking_indices, sims = query(query_repr, bounding_box_reprs)

    if verbose:
        end = timer()
        print('Localization took {:.6f} seconds'.format(end-start))
        start = timer()

    # Step 3: average query expansion
    best_rerank_indices = reranking_indices[:avg_query_exp_n]
    avg_repr = np.average(np.vstack((bounding_box_reprs[best_rerank_indices], 
                                     query_repr)), axis=0)
    exp_indices, similarity = query(avg_repr, bounding_box_reprs)

    if verbose:
        end = timer()
        print('Average query expansion took {:.6f} seconds'.format(end-start))

    # Construct bounding boxes return list
    bbox_list = []
    for feature_idx, bbox in zip(indices[exp_indices], 
                                 bounding_boxes[exp_indices]):
        # Map bounding box coordinates to image coordinates
        metadata = search_model.get_metadata(feature_idx)
        scale_x = metadata['width'] / feature_shapes[feature_idx][1]
        scale_y = metadata['height'] / feature_shapes[feature_idx][0]
        bbox_list.append((round(bbox.item(0)*scale_x), 
                          round(bbox.item(1)*scale_y), 
                          round(bbox.item(2)*scale_x), 
                          round(bbox.item(3)*scale_y)))

    return indices[exp_indices], similarity, bbox_list
