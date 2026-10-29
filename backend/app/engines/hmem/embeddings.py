# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
import math
import re


def tokenize(text):
    return [t for t in re.sub(r'[^a-z0-9\s]', ' ', text.lower()).split() if len(t) > 1]


def embed(text):
    vector = [0.0] * 64
    for token in tokenize(text):
        h = 0x811c9dc5
        for character in token:
            h = ((h ^ ord(character)) * 0x01000193) & 0xffffffff
        vector[h % 64] += (1 if (h >> 8) % 2 == 0 else -1) * (1 + ((h >> 16) % 8) / 8)
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0
