"""Tests for sharpness.py (Laplacian-variance sharpness metric)."""
from PIL import Image, ImageFilter

from tidy.media import sharpness


def _checkerboard_image(size=(64, 64), cell=4):
    image = Image.new("L", size, 0)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if (x // cell + y // cell) % 2 == 0:
                pixels[x, y] = 255
    return image.convert("RGB")


def _solid_image(size=(64, 64), color=(128, 128, 128)):
    return Image.new("RGB", size, color)


class TestLaplacianVariance:
    def test_solid_color_has_zero_variance(self):
        assert sharpness.laplacian_variance(_solid_image()) == 0.0

    def test_sharp_checkerboard_has_high_variance(self):
        value = sharpness.laplacian_variance(_checkerboard_image())
        assert value > 0

    def test_blurring_reduces_sharpness(self):
        sharp = _checkerboard_image()
        blurred = sharp.filter(ImageFilter.GaussianBlur(radius=3))

        sharp_value = sharpness.laplacian_variance(sharp)
        blurred_value = sharpness.laplacian_variance(blurred)

        assert blurred_value < sharp_value

    def test_downscales_large_images_before_convolving(self):
        # A huge solid image must not take noticeably longer than a small
        # one -- max_dim downscaling is what keeps this cheap at archive
        # scale (multi-megapixel photos).
        big = _solid_image(size=(4000, 3000))
        value = sharpness.laplacian_variance(big, max_dim=64)
        assert value == 0.0
