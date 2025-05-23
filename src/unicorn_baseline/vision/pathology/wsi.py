import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional

import cv2
import numpy as np
import tqdm
import wholeslidedata as wsd
from numba import njit
from PIL import Image

from unicorn_baseline.vision.pathology.wsi_utils import HasEnoughTissue

# ignore all warnings from wholeslidedata
warnings.filterwarnings("ignore", module="wholeslidedata")

Image.MAX_IMAGE_PIXELS = 933120000


class FilterParams(NamedTuple):
    """
    Parameters for filtering contours.
    """

    ref_tile_size: int  # reference tile size for filtering
    a_t: int  # contour area threshold for filtering
    a_h: int  # hole area threshold for filtering
    max_n_holes: int  # maximum number of holes allowed


class TilingParams(NamedTuple):
    """
    Parameters for tiling.
    """

    drop_holes: bool  # whether to drop tiles that fall within holes
    tissue_thresh: float  # minimum tissue percentage required for a tile
    use_padding: bool  # whether to use padding for tiles at the edges


class WholeSlideImage(object):
    """
    A class for handling Whole Slide Images (wsi) and tile extraction.
    Attributes:
        path (Path): Full path to the wsi.
        name (str): Name of the wsi (stem of the path).
        fmt (str): File format of the wsi.
        wsi (wsd.WholeSlideImage): wsi object.
        spacing (Optional[float]): Manually set spacing at level 0.
        spacings (List[float]): List of spacings for each level.
        level_dimensions (List[Tuple[int, int]]): Dimensions at each level.
        level_downsamples (List[Tuple[float, float]]): Downsample factors for each level.
        backend (str): Backend used for opening the wsi (default: "asap").
        mask_path (Optional[Path]): Path to the segmentation mask.
        mask (Optional[wsd.WholeSlideImage]): mask object.
        seg_level (int): Level for segmentation.
        contours_tissue (Optional[List]): Tissue contours.
        contours_tumor (Optional[List]): Tumor contours.
        binary_mask (Optional[np.ndarray]): Binary segmentation mask as a numpy array.
    """

    def __init__(
        self,
        path: Path,
        mask_path: Optional[Path] = None,
        spacing: Optional[float] = None,
        downsample: int = 32,
        backend: str = "asap",
    ):
        """
        Initializes a Whole Slide Image object with optional mask and spacing.

        Args:
            path (Path): Path to the wsi.
            mask_path (Optional[Path]): Path to the tissue mask, if available. Defaults to None.
            spacing (Optional[float]): Manually set spacing at level 0, if speficied. Defaults to None.
            downsample (int): Downsample factor for finding best level for tissue segmentation. Defaults to 64.
            backend (str): Backend to use for opening the wsi. Defaults to "asap".
        """

        self.path = path
        self.name = path.stem.replace(" ", "_")
        self.fmt = path.suffix
        self.wsi = wsd.WholeSlideImage(path, backend=backend)

        self._scaled_contours_cache = {}  # add a cache for scaled contours
        self._scaled_holes_cache = {}  # add a cache for scaled holes
        self._level_spacing_cache = {}  # add a cache for level spacings

        self.spacing = spacing  # manually set spacing at level 0
        self.spacings = self.get_spacings()
        self.level_dimensions = self.wsi.shapes
        self.level_downsamples = self.get_downsamples()
        self.backend = backend

        self.mask_path = mask_path
        if mask_path is not None:
            self.mask = wsd.WholeSlideImage(mask_path, backend=backend)
            self.seg_level = self.load_segmentation(downsample)
        else:
            self.seg_level = self.segment_tissue(downsample)

        self.contours_tissue = None
        self.contours_tumor = None

    def get_downsamples(self):
        """
        Calculate the downsample factors for each level of the image pyramid.

        This method computes the downsample factors for each level in the image
        pyramid relative to the base level (level 0). The downsample factor for
        each level is represented as a tuple of two values, corresponding to the
        downsampling in the width and height dimensions.

        Returns:
            list of tuple: A list of tuples where each tuple contains two float
            values representing the downsample factors (width_factor, height_factor)
            for each level relative to the base level.
        """
        level_downsamples = []
        dim_0 = self.level_dimensions[0]
        for dim in self.level_dimensions:
            level_downsample = (dim_0[0] / float(dim[0]), dim_0[1] / float(dim[1]))
            level_downsamples.append(level_downsample)
        return level_downsamples

    def get_spacings(self):
        """
        Retrieve the spacings for the whole slide image.

        If the `spacing` attribute is not set, the method returns the original spacings
        from the wsi. Otherwise, it calculates adjusted spacings based on the provided
        `spacing` value and the original spacings.

        Returns:
            list: A list of spacings, either the original or adjusted based on the
            `spacing` attribute.
        """
        if self.spacing is None:
            spacings = self.wsi.spacings
        else:
            spacings = [
                self.spacing * s / self.wsi.spacings[0] for s in self.wsi.spacings
            ]
        return spacings

    def get_level_spacing(self, level: int):
        """
        Retrieve the spacing value for a specified level.

        Args:
            level (int): Level for which to retrieve the spacing.

        Returns:
            float: Spacing value corresponding to the specified level.
        """
        if level not in self._level_spacing_cache:
            self._level_spacing_cache[level] = self.spacings[level]
        return self._level_spacing_cache[level]

    def get_best_level_for_spacing(self, target_spacing: float, tolerance: float = 0.1):
        """
        Determines the best level in a multi-resolution image pyramid for a given target spacing.

        This method identifies the optimal level in the image pyramid that most closely matches
        the desired target spacing. If the closest natural spacing at a given level is within a
        specified tolerance (as a percentage) of the target spacing, that level is selected, and
        the natural spacing is used. Otherwise, the method selects the closest smaller spacing
        available in the pyramid.

        Args:
            target_spacing (float): Desired spacing.
            tolerance (float, optional): The tolerance level (as a percentage of the target spacing)
                                         within which a natural spacing is considered acceptable.

        Returns:
            level (int): Index of the best matching level in the image pyramid.
        """
        spacing = self.get_level_spacing(0)
        target_downsample = target_spacing / spacing
        level = self.get_best_level_for_downsample_custom(target_downsample)
        level_spacing = self.get_level_spacing(level)
        # if level_spacing is bigger than target spacing but within tolerance, return this level
        if abs(level_spacing - target_spacing) / target_spacing <= tolerance:
            return level
        # if level spacing is bigger than target spacing, find the next level with a smaller spacing
        else:
            if level_spacing > target_spacing:
                while level > 0 and level_spacing > target_spacing and not abs(level_spacing - target_spacing) / target_spacing <= tolerance:
                    level -= 1
                    level_spacing = self.get_level_spacing(level)
            assert (
                level_spacing <= target_spacing or abs(level_spacing - target_spacing) / target_spacing <= tolerance
            ), f"Could not find a level with spacing smaller than or equal to {target_spacing} or within a tolerance of {tolerance * 100}%"
            return level

    def get_best_level_for_downsample_custom(self, downsample: float | int):
        """
        Determines the best level for a given downsample factor based on the available
        level downsample values, with an optional tolerance check.

        Args:
            downsample (float): Target downsample factor.
            tol (float, optional): The tolerance level for the downsample factor match.
                Defaults to 0.1.
            return_tol_status (bool, optional): If True, returns whether the selected
                level's downsample factor is within the specified tolerance. Defaults to False.

        Returns:
            int: Index of the best matching level for the given downsample factor.
            float (optional): Tolerance value (only if `return_tol_status` is True).
            bool (optional): Whether the selected level's downsample factor exceeds the
                specified tolerance (only if `return_tol_status` is True).
        """
        level = int(np.argmin([abs(x - downsample) for x, _ in self.level_downsamples]))
        return level

    def load_segmentation(
        self,
        downsample: int,
        sthresh_up: int = 255,
        tissue_pixel_value: int = 1,
    ):
        """
        Load and process a segmentation mask for a whole slide image.

        This method ensures that the segmentation mask and the slide have at least one
        common spacing, determines the best level for the given downsample factor, and
        processes the segmentation mask to create a binary mask.

        Args:
            downsample (int): Downsample factor for finding best level for tissue segmentation.
            sthresh_up (int, optional): Upper threshold value for scaling the binary
                mask. Defaults to 255.
            tissue_pixel_value (int, optional): Pixel value in the segmentation mask that
                represents tissue. Defaults to 1.

        Returns:
            int: Level at which the tissue mask was loaded.
        """
        mask_spacing_at_level_0 = self.mask.spacings[0]
        seg_level = self.get_best_level_for_downsample_custom(downsample)
        seg_spacing = self.get_level_spacing(seg_level)
        while seg_spacing < mask_spacing_at_level_0 and seg_level < len(self.spacings) - 1:
            seg_level += 1
            seg_spacing = self.get_level_spacing(seg_level)

        assert seg_spacing >= mask_spacing_at_level_0, f"Segmentation mask natural spacing ({mask_spacing_at_level_0}) is higher than the lowest spacing in the slide ({seg_spacing}), aborting."

        mask_downsample = seg_spacing / mask_spacing_at_level_0
        mask_level = int(np.argmin([abs(x - mask_downsample) for x in self.mask.downsamplings]))
        mask_spacing = self.mask.spacings[mask_level]

        scale = seg_spacing / mask_spacing
        while scale < 1 and mask_level > 0:
            mask_level -= 1
            mask_spacing = self.mask.spacings[mask_level]
            scale = seg_spacing / mask_spacing

        assert scale >= 1
        mask = self.mask.get_slide(spacing=mask_spacing)
        width, height, _ = mask.shape
        # resize the mask to the size of the slide at seg_spacing
        mask = cv2.resize(mask.astype(np.uint8), (int(height // scale), int(width // scale)), interpolation=cv2.INTER_NEAREST)

        m = (mask == tissue_pixel_value).astype("uint8")
        if np.max(m) <= 1:
            m = m * sthresh_up

        self.binary_mask = m
        return seg_level

    def segment_tissue(
        self,
        downsample: int,
        sthresh: int = 20,
        sthresh_up: int = 255,
        mthresh: int = 7,
        close: int = 0,
        use_otsu: bool = False,
    ):
        """
        Segment the tissue via HSV -> Median thresholding -> Binary thresholding -> Morphological closing.

        Args:
            downsample (int): Downsample factor for finding best level for tissue segmentation.
            sthresh (int, optional): Lower threshold for binary thresholding. Defaults to 20.
            sthresh_up (int, optional): Upper threshold for binary thresholding. Defaults to 255.
            mthresh (int, optional): Kernel size for median blurring. Defaults to 7.
            close (int, optional): Size of the kernel for morphological closing.
                If 0, no morphological closing is applied. Defaults to 0.
            use_otsu (bool, optional): Whether to use Otsu's method for thresholding. Defaults to False.

        Returns:
            int: Level at which the tissue mask was created.
        """

        seg_level = self.get_best_level_for_downsample_custom(downsample)
        seg_spacing = self.get_level_spacing(seg_level)

        img = self.wsi.get_slide(spacing=seg_spacing)
        img = np.array(Image.fromarray(img).convert("RGBA"))
        img_hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)  # convert to HSV space
        img_med = cv2.medianBlur(img_hsv[:, :, 1], mthresh)  # apply median blurring

        # thresholding
        if use_otsu:
            _, img_thresh = cv2.threshold(
                img_med, 0, sthresh_up, cv2.THRESH_OTSU + cv2.THRESH_BINARY
            )
        else:
            _, img_thresh = cv2.threshold(
                img_med, sthresh, sthresh_up, cv2.THRESH_BINARY
            )

        # morphological closing
        if close > 0:
            kernel = np.ones((close, close), np.uint8)
            img_thresh = cv2.morphologyEx(img_thresh, cv2.MORPH_CLOSE, kernel)

        self.binary_mask = img_thresh
        return seg_level

    def get_tile_coordinates(
        self,
        target_spacing,
        target_tile_size,
        overlap=0.0,
        tiling_params: TilingParams = TilingParams(
            drop_holes=False, tissue_thresh=0.25, use_padding=True
        ),
        filter_params: FilterParams = FilterParams(
            ref_tile_size=256, a_t=4, a_h=2, max_n_holes=8
        ),
        tolerance: float = 0.1,
        num_workers: int = 1,
    ):
        """
        Extract tile coordinates based on the specified target spacing, tile size, overlap,
        and additional tiling and filtering parameters.

        Args:
            target_spacing (float): Desired spacing of the tiles.
            target_tile_size (int): Desired size of the tiles at the target spacing.
            overlap (float, optional): Overlap between adjacent tiles. Defaults to 0.0.
            tiling_params (NamedTuple, optional): Parameters for tiling, including:
                - "drop_holes" (bool): If True, tiles falling within a hole will be excluded. Defaults to False.
                - "tissue_thresh" (float, optional): Minimum amount pixels covered with tissue required for a tile. Defaults to 0.25 (25 percent).
                - "use_padding" (bool): Whether to use padding for tiles at the edges. Defaults to True.
            filter_params (NamedTuple, optional): Parameters for filtering contours, including:
                - "ref_tile_size" (int): Reference tile size for filtering. Defaults to 256.
                - "a_t" (int): Contour area threshold for filtering. Defaults to 4.
                - "a_h" (int): Hole area threshold for filtering. Defaults to 2.
                - "max_n_holes" (int): Maximum number of holes allowed. Defaults to 8.
            tolerance (float, optional): Tolerance for spacing matching. Defaults to 0.1.
            num_workers (int, optional): Number of workers to use for parallel processing.
                Defaults to 1.

        Returns:
            Tuple:
                - tile_coordinates (List[Tuple[int, int]]): List of (x, y) coordinates for the extracted tiles.
                - tissue_percentages (List[float]): List of tissue percentages for each tile.
                - tile_level (int): Level of the wsi used for tile extraction.
                - resize_factor (float): The factor by which the tile size was resized.
                - tile_size_lv0 (int): The tile size at level 0 of the wsi pyramid.
        """
        scale = target_spacing / self.get_level_spacing(0)
        tile_size_lv0 = int(target_tile_size * scale)

        contours, holes = self.detect_contours(target_spacing, filter_params)
        (
            running_x_coords,
            running_y_coords,
            tissue_percentages,
            tile_level,
            resize_factor,
        ) = self.process_contours(
            contours,
            holes,
            spacing=target_spacing,
            tile_size=target_tile_size,
            overlap=overlap,
            drop_holes=tiling_params.drop_holes,
            tissue_thresh=tiling_params.tissue_thresh,
            use_padding=tiling_params.use_padding,
            tolerance=tolerance,
            num_workers=num_workers,
        )
        tile_coordinates = list(zip(running_x_coords, running_y_coords))
        return (
            tile_coordinates,
            tissue_percentages,
            tile_level,
            resize_factor,
            tile_size_lv0,
        )

    @staticmethod
    def filter_contours(contours, hierarchy, filter_params: FilterParams):
        """
        Filter contours by area using FilterParams.
        """
        filtered = []

        # find indices of foreground contours (parent == -1)
        hierarchy_1 = np.flatnonzero(hierarchy[:, 1] == -1)
        all_holes = []

        # loop through foreground contour indices
        for cont_idx in hierarchy_1:
            # actual contour
            cont = contours[cont_idx]
            # indices of holes contained in this contour (children of parent contour)
            holes = np.flatnonzero(hierarchy[:, 1] == cont_idx)
            # take contour area (includes holes)
            a = cv2.contourArea(cont)
            # calculate the contour area of each hole
            hole_areas = [cv2.contourArea(contours[hole_idx]) for hole_idx in holes]
            # actual area of foreground contour region
            a = a - np.array(hole_areas).sum()
            if a == 0:
                continue
            if a > filter_params.a_t:  # Use named tuple instead of dictionary
                filtered.append(cont_idx)
                all_holes.append(holes)

        foreground_contours = [contours[cont_idx] for cont_idx in filtered]

        hole_contours = []
        for hole_ids in all_holes:
            unfiltered_holes = [contours[idx] for idx in hole_ids]
            unfilered_holes = sorted(
                unfiltered_holes, key=cv2.contourArea, reverse=True
            )
            # take max_n_holes largest holes by area
            unfilered_holes = unfilered_holes[
                : filter_params.max_n_holes
            ]  # Use named tuple
            filtered_holes = []

            # filter these holes
            for hole in unfilered_holes:
                if cv2.contourArea(hole) > filter_params.a_h:  # Use named tuple
                    filtered_holes.append(hole)

            hole_contours.append(filtered_holes)

        return foreground_contours, hole_contours

    def detect_contours(
        self,
        target_spacing: float,
        filter_params: FilterParams,
    ):
        """
        Detect and filter contours from a binary mask based on specified parameters.

        This method identifies contours in a binary mask, filters them based on area
        thresholds, and scales the contours to a specified target resolution.

        Args:
            target_spacing (float): Desired spacing at which tiles should be extracted.
            filter_params (NamedTuple): A NamedTuple containing filtering parameters:
                - "a_t" (int): Minimum area threshold for foreground contours.
                - "a_h" (int): Minimum area threshold for holes within contours.
                - "max_n_holes" (int): Maximum number of holes to retain per contour.
                - "ref_tile_size" (int): Reference tile size for computing areas.

        Returns:
            Tuple[List[np.ndarray], List[List[np.ndarray]]]:
                - A list of scaled foreground contours.
                - A list of lists containing scaled hole contours for each foreground contour.
        """

        spacing_level = self.get_best_level_for_spacing(target_spacing)
        current_scale = self.level_downsamples[spacing_level]
        target_scale = self.level_downsamples[self.seg_level]
        scale = tuple(a / b for a, b in zip(target_scale, current_scale))
        ref_tile_size = filter_params.ref_tile_size  # Use named tuple
        scaled_ref_tile_area = int(ref_tile_size**2 / (scale[0] * scale[1]))

        _filter_params = FilterParams(
            ref_tile_size=filter_params.ref_tile_size,
            a_t=filter_params.a_t * scaled_ref_tile_area,
            a_h=filter_params.a_h * scaled_ref_tile_area,
            max_n_holes=filter_params.max_n_holes,
        )

        # find and filter contours
        contours, hierarchy = cv2.findContours(
            self.binary_mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE
        )
        hierarchy = np.squeeze(hierarchy, axis=(0,))[:, 2:]

        foreground_contours, hole_contours = self.filter_contours(
            contours, hierarchy, _filter_params
        )

        # scale detected contours to level 0
        contours = self.scaleContourDim(foreground_contours, target_scale)
        holes = self.scaleHolesDim(hole_contours, target_scale)
        return contours, holes

    @staticmethod
    @njit
    def isInHoles(holes, pt, tile_size):
        """
        Check if a given tile is inside any of the specified polygonal holes.

        Args:
            holes (list): A list of polygonal contours, where each contour is represented
                        as a list of points (e.g., from OpenCV's findContours function).
            pt (tuple): The (x, y) coordinates of the top-left corner of the tile to check.
            tile_size (int or float): The size of the tile, used to calculate the center
                                    of the point being tested.

        Returns:
            int: Returns 1 if the point is inside any of the holes, otherwise returns 0.
        """
        for hole in holes:
            if (
                cv2.pointPolygonTest(
                    hole, (pt[0] + tile_size / 2, pt[1] + tile_size / 2), False
                )
                > 0
            ):
                return 1

        return 0

    @staticmethod
    def isInContours(cont_check_fn, pt, holes=None, drop_holes=True, tile_size=256):
        """
        Determines whether a given tile is within contours (and optionally outside of holes).

        Args:
            cont_check_fn (callable): A function that checks if a tile is within contours.
                It should accept a (x,y) coordinates as input and return a tuple (keep_flag, tissue_pct),
                where `keep_flag` is a boolean indicating if the tile is within contours,
                and `tissue_pct` is the percentage of tissue coverage of the tile.
            pt (tuple): The (x, y) coordinates of the top-left corner of the tile to check.
            holes (list, optional): A list of holes (e.g., regions to exclude) to check against.
                Defaults to None.
            drop_holes (bool, optional): If True, tiles falling within a hole will be excluded.
                Defaults to True.
            tile_size (int, optional): The size of the tile to consider.
                Defaults to 256.

        Returns:
            tuple: A tuple (keep_flag, tissue_pct), where:
                - `keep_flag` is 1 if the tile is within contours and not in holes (if applicable),
                  otherwise 0.
                - `tissue_pct` is the percentage of tissue coverage of the tile.
        """
        keep_flag, tissue_pct = cont_check_fn(pt)
        if keep_flag:
            if holes is not None and drop_holes:
                return not WholeSlideImage.isInHoles(holes, pt, tile_size), tissue_pct
            else:
                return 1, tissue_pct
        return 0, tissue_pct

    @staticmethod
    def scaleContourDim(contours, scale):
        """
        Scales the dimensions of a list of contours by a given factor.

        Args:
            contours (list of numpy.ndarray): A list of contours, where each contour is
                represented as a numpy array of coordinates.
            scale (float): The scaling factor to apply to the contours.

        Returns:
            list of numpy.ndarray: A list of scaled contours, where each contour's
            coordinates are multiplied by the scaling factor and converted to integers.
        """
        return [np.array(cont * scale, dtype="int32") for cont in contours]

    @staticmethod
    def scaleHolesDim(contours, scale):
        """
        Scales the dimensions of holes within a set of contours by a given factor.

        Args:
            contours (list of list of numpy.ndarray): A list of contours, where each contour
                is represented as a list of holes, and each hole is a numpy array of coordinates.
            scale (float): The scaling factor to apply to the dimensions of the holes.

        Returns:
            list of list of numpy.ndarray: A new list of contours with the dimensions of
            the holes scaled by the specified factor.
        """
        return [
            [np.array(hole * scale, dtype="int32") for hole in holes]
            for holes in contours
        ]

    def process_contours(
        self,
        contours,
        holes,
        spacing: float = 0.5,
        tile_size: int = 256,
        overlap: float = 0.0,
        drop_holes: bool = True,
        tissue_thresh: float = 0.01,
        use_padding: bool = True,
        tolerance: float = 0.1,
        num_workers: int = 1,
    ):
        """
        Processes a list of contours and their corresponding holes to generate tile coordinates,
        tissue percentages, and other metadata.

        Args:
            contours (list): List of contours representing tissue blobs in the wsi.
            holes (list): List of tissue holes in each contour.
            spacing (float, optional): Desired spacing for tiling. Defaults to 0.5.
            tile_size (int, optional): Desired tile size in pixels. Defaults to 256.
            overlap (float, optional): Overlap between adjacent tiles. Defaults to 0.0.
            drop_holes (bool, optional): Whether to drop tiles that fall within holes. Defaults to True.
            tissue_thresh (float, optional): Minimum amount pixels covered with tissue required for a tile. Defaults to 0.01 (1 percent).
            use_padding (bool, optional): Whether to pad the tiles to ensure full coverage. Defaults to True.
            tolerance (float, optional): Tolerance for spacing matching. Defaults to 0.1.
            num_workers (int, optional): Number of workers to use for parallel processing. Defaults to 1.

        Returns:
            tuple: A tuple containing:
                - running_x_coords (list): The x-coordinates of the extracted tiles.
                - running_y_coords (list): The y-coordinates of the extracted tiles.
                - running_tissue_pct (list): List of tissue percentages for each extracted tile.
                - tile_level (int): Level of the wsi used for tile extraction.
                - resize_factor (float): The factor by which the tile size was resized.
        """
        running_x_coords, running_y_coords = [], []
        running_tissue_pct = []
        tile_level = None
        resize_factor = None

        def process_single_contour(i):
            return self.process_contour(
                contours[i],
                holes[i],
                spacing,
                tile_size,
                overlap,
                drop_holes,
                tissue_thresh,
                use_padding,
                tolerance,
            )

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = list(
                tqdm.tqdm(
                    executor.map(process_single_contour, range(len(contours))),
                    desc=f"Extracting tissue tiles",
                    unit=" tissue blob",
                    total=len(contours),
                    leave=True,
                    file=sys.stdout,
                )
            )

        for (
            x_coords,
            y_coords,
            tissue_pct,
            cont_tile_level,
            cont_resize_factor,
        ) in results:
            if len(x_coords) > 0:
                if tile_level is not None:
                    assert (
                        tile_level == cont_tile_level
                    ), "Tile level should be the same for all contours"
                tile_level = cont_tile_level
                resize_factor = cont_resize_factor
                running_x_coords.extend(x_coords)
                running_y_coords.extend(y_coords)
                running_tissue_pct.extend(tissue_pct)

        return (
            running_x_coords,
            running_y_coords,
            running_tissue_pct,
            tile_level,
            resize_factor,
        )

    def process_contour(
        self,
        contour,
        contour_holes,
        spacing: float,
        tile_size: int = 256,
        overlap: float = 0.0,
        drop_holes: bool = True,
        tissue_thresh: float = 0.01,
        use_padding: bool = True,
        tolerance: float = 0.1,
    ):
        """
        Processes a contour to generate tile coordinates and associated metadata.

        Args:
            contour (numpy.ndarray): Contour to process, defined as a set of points.
            contour_holes (list): List of holes within the contour.
            spacing (float): Target spacing for the tiles.
            tile_size (int, optional): Size of the tiles in pixels. Defaults to 256.
            overlap (float, optional): Overlap between tiles. Defaults to 0.0.
            drop_holes (bool, optional): Whether to drop tiles that fall within holes. Defaults to True.
            tissue_thresh (float, optional): Minimum amount pixels covered with tissue required for a tile. Defaults to 0.01 (1 percent).
            use_padding (bool, optional): Whether to pad the image to ensure full coverage. Defaults to True.
            tolerance (float, optional): Tolerance for spacing matching. Defaults to 0.1.

        Returns:
            tuple: A tuple containing:
                - x_coords (list): List of x-coordinates for each tile.
                - y_coords (list): List of y-coordinates for each tile.
                - filtered_tissue_percentages (list): List of tissue percentages for each tile.
                - tile_level (int): Level of the image used for tile extraction.
                - resize_factor (float): The factor by which the tile size was resized.
        """
        tile_level = self.get_best_level_for_spacing(spacing, tolerance=tolerance)
        tile_spacing = self.get_level_spacing(tile_level)
        resize_factor = spacing / tile_spacing
        if resize_factor < 1:
            assert abs(tile_spacing - spacing) / spacing <= tolerance, (
                f"Spacing {tile_spacing} is not within tolerance {tolerance} of target spacing {spacing}"
            )
            # set resize_factor to 1.0 if the target spacing is smaller than the tile spacing
            # but within tolerance, to avoid upsampling the tile size
            resize_factor = 1.0

        tile_size_resized = int(tile_size * resize_factor)
        step_size = int(tile_size_resized * (1.0 - overlap))

        if contour is not None:
            start_x, start_y, w, h = cv2.boundingRect(contour)
        else:
            start_x, start_y, w, h = (
                0,
                0,
                self.level_dimensions[tile_level][0],
                self.level_dimensions[tile_level][1],
            )

        tile_downsample = (
            int(self.level_downsamples[tile_level][0]),
            int(self.level_downsamples[tile_level][1]),
        )
        ref_tile_size = (
            tile_size_resized * tile_downsample[0],
            tile_size_resized * tile_downsample[1],
        )

        img_w, img_h = self.level_dimensions[0]
        if use_padding:
            stop_y = int(start_y + h)
            stop_x = int(start_x + w)
        else:
            stop_y = min(start_y + h, img_h - ref_tile_size[1] + 1)
            stop_x = min(start_x + w, img_w - ref_tile_size[0] + 1)

        scale = self.level_downsamples[self.seg_level]
        cont = self.scaleContourDim([contour], (1.0 / scale[0], 1.0 / scale[1]))[0]

        tissue_checker = HasEnoughTissue(
            contour=cont,
            contour_holes=contour_holes,
            tissue_mask=self.binary_mask,
            tile_size=ref_tile_size[0],
            scale=scale,
            pct=tissue_thresh,
        )

        ref_step_size_x = int(step_size * tile_downsample[0])
        ref_step_size_y = int(step_size * tile_downsample[1])

        x_range = np.arange(start_x, stop_x, step=ref_step_size_x)
        y_range = np.arange(start_y, stop_y, step=ref_step_size_y)
        x_coords, y_coords = np.meshgrid(x_range, y_range, indexing="ij")
        coord_candidates = np.array(
            [x_coords.flatten(), y_coords.flatten()]
        ).transpose()

        # vectorized processing of coordinates using the tissue_checker
        keep_flags, tissue_pcts = tissue_checker.check_coordinates(coord_candidates)

        if drop_holes:
            keep_flags = [
                flag and not self.isInHoles(contour_holes, coord, ref_tile_size[0])
                for flag, coord in zip(keep_flags, coord_candidates)
            ]

        filtered_coordinates = coord_candidates[np.array(keep_flags) == 1]
        filtered_tissue_percentages = np.array(tissue_pcts)[np.array(keep_flags) == 1]

        ntile = len(filtered_coordinates)

        if ntile > 0:
            x_coords = list(filtered_coordinates[:, 0])
            y_coords = list(filtered_coordinates[:, 1])
            return (
                x_coords,
                y_coords,
                filtered_tissue_percentages,
                tile_level,
                resize_factor,
            )

        else:
            return [], [], [], None, None

    @staticmethod
    def process_coord_candidate(
        coord, contour_holes, tile_size, cont_check_fn, drop_holes
    ):
        """
        Processes a candidate coordinate to determine if it should be kept based on
        its location relative to contours and the percentage of tissue it contains.

        Args:
            coord (tuple): (x, y) coordinate to be processed.
            contour_holes (list): A list of contours and holes to check against.
            tile_size (int): Size of the tile to consider.
            cont_check_fn (callable): A function to check if the coordinate is within
                the contours or holes.
            drop_holes (bool): A flag indicating whether to drop tiles falling in holes during the check.

        Returns:
            tuple: A tuple containing:
                - coord (tuple or None): Input coordinate if it passes the check,
                otherwise None.
                - tissue_pct (float): Percentage of tissue in the tile.
        """
        keep_flag, tissue_pct = WholeSlideImage.isInContours(
            cont_check_fn, coord, contour_holes, drop_holes, tile_size
        )
        if keep_flag:
            return coord, tissue_pct
        else:
            return None, tissue_pct

    def get_tile(self, x, y, tile_size, spacing):
        """
        Extracts a tile from a whole slide image at the specified coordinates, size, and spacing.

        Args:
            x (int): The x-coordinate of the top-left corner of the tile.
            y (int): The y-coordinate of the top-left corner of the tile.
            tile_size (tuple): A tuple (width, height) specifying the dimensions of the tile.
            spacing (float): The spacing (resolution) at which the tile should be extracted.

        Returns:
            numpy.ndarray: The extracted tile as a numpy array.
        """
        return self.wsi.get_patch(
            x,
            y,
            tile_size[0],
            tile_size[1],
            spacing=spacing,
            center=False,
        )
