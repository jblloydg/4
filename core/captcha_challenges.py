"""Randomized captcha challenges for django-simple-captcha.

Each captcha picks a random method: distorted letters or a small math
puzzle, so the expected way of answering changes from one check to the next.
Runs entirely on the Find-IT server (no outside service or internet keys).
"""
import secrets

from captcha.helpers import math_challenge, random_char_challenge


def random_challenge():
    return secrets.choice([random_char_challenge, math_challenge])()
