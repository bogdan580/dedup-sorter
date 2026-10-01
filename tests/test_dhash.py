"""Tests for dhash.py (perceptual hash)."""
from PIL import Image

from tidy.media import dhash


def _solid_image(size=(64, 64), color=(120, 120, 120)):
    return Image.new("RGB", size, color)


def _checkerboard_image(size=(64, 64)):
    image = Image.new("RGB", size, (0, 0, 0))
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if (x // 8 + y // 8) % 2 == 0:
                pixels[x, y] = (255, 255, 255)
    return image


class TestComputeDhash:
    def test_returns_64_bit_int(self):
        value = dhash.compute_dhash(_solid_image())
        assert isinstance(value, int)
        assert 0 <= value < (1 << 64)

    def test_identical_images_produce_identical_hash(self):
        a = dhash.compute_dhash(_checkerboard_image())
        b = dhash.compute_dhash(_checkerboard_image())
        assert a == b

    def test_solid_color_has_zero_hamming_distance_to_itself(self):
        value = dhash.compute_dhash(_solid_image(color=(200, 10, 10)))
        assert dhash.hamming_distance(value, value) == 0

    def test_very_different_images_have_large_hamming_distance(self):
        solid = dhash.compute_dhash(_solid_image(color=(128, 128, 128)))
        checker = dhash.compute_dhash(_checkerboard_image())
        assert dhash.hamming_distance(solid, checker) > 20

    def test_slightly_recompressed_image_stays_close(self):
        import io
        base = _checkerboard_image()
        buf = io.BytesIO()
        base.save(buf, "JPEG", quality=70)
        buf.seek(0)
        recompressed = Image.open(buf)

        a = dhash.compute_dhash(base)
        b = dhash.compute_dhash(recompressed)
        assert dhash.hamming_distance(a, b) <= 6


class TestHammingDistance:
    def test_identical_values_zero_distance(self):
        assert dhash.hamming_distance(0b1010, 0b1010) == 0

    def test_all_bits_different(self):
        assert dhash.hamming_distance(0, 0xFFFFFFFFFFFFFFFF) == 64

    def test_single_bit_difference(self):
        assert dhash.hamming_distance(0b0000, 0b0001) == 1

    def test_symmetric(self):
        assert dhash.hamming_distance(0b1100, 0b0011) == dhash.hamming_distance(0b0011, 0b1100)
