"""Compact skeleton trails: byte adjacency/visitation, no per-pixel Python graph."""
import math

import numpy as np

from .reference_images import check_cancel

MAX_NODES = 2_000_000
MAX_RAW_PATHS = 100_000
DIRECTIONS = ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))
BITS = tuple(1 << i for i in range(8))
CHOICES = tuple(tuple(i for i in range(8) if value & BITS[i]) for value in range(256))
DEGREES = np.array([len(row) for row in CHOICES], np.uint8)
NEXT = tuple(tuple(max(choices, key=lambda i: (
    (DIRECTIONS[last][0]*DIRECTIONS[i][0] + DIRECTIONS[last][1]*DIRECTIONS[i][1]) /
    math.hypot(*DIRECTIONS[i]), -i)) if choices else -1 for choices in CHOICES) for last in range(8))


def trails(mask, cancel=None):
    from .trace_budget import ComplexityLimit
    check_cancel(cancel)
    count = int(np.count_nonzero(mask))
    if count > MAX_NODES:
        raise ComplexityLimit('pixels', count, MAX_NODES)
    h, w = mask.shape
    padded = np.pad(mask > 0, 1)
    active = padded[1:h+1, 1:w+1]
    adjacency = np.zeros((h, w), np.uint8)
    for direction, (dy, dx) in enumerate(DIRECTIONS):
        check_cancel(cancel)
        connected = active & padded[1+dy:1+dy+h, 1+dx:1+dx+w]
        if dy and dx:
            connected &= ~(padded[1:1+h, 1+dx:1+dx+w] | padded[1+dy:1+dy+h, 1:1+w])
        adjacency[connected] |= BITS[direction]
    flat = adjacency.ravel()
    degrees = DEGREES[flat]
    visited = np.zeros(flat.shape, np.uint8)
    offsets = tuple(dy*w+dx for dy, dx in DIRECTIONS)
    # Start at odd endpoints/junctions, then consume the remaining cycles. At
    # junctions keep going along an available actual edge, preferring straight.
    # This joins fragments without inserting or erasing any skeleton edges.
    starts = np.flatnonzero(degrees & 1)
    others = np.flatnonzero((degrees > 0) & ((degrees & 1) == 0))
    paths = []
    traversed = 0
    for nodes in (starts, others):
        for index, value in enumerate(nodes):
            if index % 4096 == 0: check_cancel(cancel)
            node = int(value)
            todo = int(flat[node]) & ~int(visited[node])
            while todo:
                direction = CHOICES[todo][0]
                current = node
                chain = [node]
                while direction >= 0:
                    other = current+offsets[direction]
                    visited[current] |= BITS[direction]
                    visited[other] |= BITS[(direction+4) % 8]
                    chain.append(other)
                    traversed += 1
                    if traversed % 4096 == 0: check_cancel(cancel)
                    current = other
                    remaining = int(flat[current]) & ~int(visited[current])
                    direction = NEXT[direction][remaining]
                chain = np.asarray(chain, np.int32)
                points = np.column_stack((chain % w, chain // w))
                if len(points) > 2:
                    steps = np.diff(points, axis=0)
                    cross = steps[:-1, 0]*steps[1:, 1]-steps[:-1, 1]*steps[1:, 0]
                    dot = np.sum(steps[:-1]*steps[1:], axis=1)
                    retain = np.r_[True, (cross != 0) | (dot <= 0), True]
                    points = points[retain]  # Exact collinear compression, zero geometric error.
                paths.append([(float(x), float(y)) for x, y in points])
                if len(paths) > MAX_RAW_PATHS:
                    raise ComplexityLimit('raw_paths', len(paths), MAX_RAW_PATHS)
                todo = int(flat[node]) & ~int(visited[node])
    check_cancel(cancel)
    return paths
