import os
import time

from krita import Document, Krita, Node, QImage, QObject, QTimer, Selection, pyqtSignal

from .client import Client
from .config import Config
from .defaults import (
    ADD_MASK_TIMEOUT,
    ERR_NO_DOCUMENT,
    STATE_IMG2IMG,
    STATE_INIT,
    STATE_INPAINT,
    STATE_READY,
    STATE_RESET_DEFAULT,
    STATE_TXT2IMG,
    STATE_UPSCALE,
    STATE_URLERROR,
    STATE_WAIT,
)
from .utils import (
    b64_to_img,
    create_layer,
    find_optimal_selection_region,
    img_to_ba,
    save_img,
)


# Does it actually have to be a QObject?
# The only possible use I see is for event emitting
class Script(QObject):
    cfg: Config
    """config singleton"""
    client: Client
    """API client singleton"""
    status: str
    """Current status (shown in status bar)"""
    app: Krita
    """Krita's Application instance (KDE Application)"""
    doc: Document
    """Currently opened document if any"""
    node: Node
    """Currently selected layer in Krita"""
    selection: Selection
    """Selection region in Krita"""
    x: int
    """Left position of selection"""
    y: int
    """Top position of selection"""
    width: int
    """Width of selection"""
    height: int
    """Height of selection"""
    status_changed = pyqtSignal(str)

    # NOTE: using property getters should be the exception, not the norm
    @property
    def selection_image(self) -> QImage:
        """QImage of selection"""
        return QImage(
            self.doc.pixelData(self.x, self.y, self.width, self.height),
            self.width,
            self.height,
            QImage.Format_RGBA8888,
        ).rgbSwapped()

    @property
    def mask_image(self) -> QImage:
        """QImage of mask layer"""
        return QImage(
            self.node.pixelData(self.x, self.y, self.width, self.height),
            self.width,
            self.height,
            QImage.Format_RGBA8888,
        ).rgbSwapped()

    def __init__(self):
        super(Script, self).__init__()
        # Persistent settings (should reload between Krita sessions)
        self.cfg = Config()
        self.client = Client(self.cfg)
        self.client.status.connect(self.status_changed.emit)

    def restore_defaults(self, if_empty=False):
        """Restore to default config."""
        self.cfg.restore_defaults(not if_empty)

        if not if_empty:
            self.status_changed.emit(STATE_RESET_DEFAULT)

    def update_selection(self):
        """Update references to key Krita objects as well as selection information."""
        self.app = Krita.instance()
        self.doc = self.app.activeDocument()

        # self.doc doesnt exist at app startup
        if not self.doc:
            self.status_changed.emit(ERR_NO_DOCUMENT)
            return

        self.node = self.doc.activeNode()
        self.selection = self.doc.selection()

        is_not_selected = (
            self.selection is None
            or self.selection.width() < 1
            or self.selection.height() < 1
        )
        if is_not_selected:
            self.x = 0
            self.y = 0
            self.width = self.doc.width()
            self.height = self.doc.height()
        else:
            self.x = self.selection.x()
            self.y = self.selection.y()
            self.width = self.selection.width()
            self.height = self.selection.height()

    def adjust_selection(self):
        """Adjust selection region to account for scaling and striding to prevent image stretch."""
        if self.selection is not None and self.cfg("fix_aspect_ratio", bool):
            x, y, width, height = find_optimal_selection_region(
                self.cfg("sd_base_size", int),
                self.cfg("sd_max_size", int),
                self.x,
                self.y,
                self.width,
                self.height,
                self.doc.width(),
                self.doc.height(),
            )

            self.x = x
            self.y = y
            self.width = width
            self.height = height

    def update_config(self):
        """Update certain config/state from the backend."""
        return self.client.get_config()

    def insert_img(self, layer_name, enc, visible=True):
        """Insert image as new layer."""
        image = b64_to_img(enc)
        ba = img_to_ba(image)

        layer = create_layer(self.doc, layer_name)
        layer.setVisible(visible)

        layer.setPixelData(ba, self.x, self.y, self.width, self.height)
        print(f"Inserted image: {enc}")

    def apply_txt2img(self):
        response = self.client.post_txt2img(
            self.width, self.height, self.selection is not None
        )
        assert response is not None, "Backend Error, check terminal"
        outputs = response["outputs"]
        print(f"Getting images: {outputs}")
        for i, output in enumerate(outputs):
            self.insert_img(f"txt2img {i + 1}", output)
        self.doc.refreshProjection()

    def apply_img2img(self, mode):
        path = os.path.join(self.cfg("sample_path", str), f"{int(time.time())}.png")
        mask_path = os.path.join(
            self.cfg("sample_path", str), f"{int(time.time())}_mask.png"
        )
        mask_path = self.cfg("new_img_mask_path", str)
        if mode == 1:
            if self.cfg("save_temp_images", bool):
                save_img(self.mask_image, mask_path)
            # auto-hide mask layer before getting selection image
            self.node.setVisible(False)
            self.doc.refreshProjection()

        if self.cfg("save_temp_images", bool):
            save_img(self.selection_image, path)

        response = (
            self.client.post_inpaint(
                self.selection_image, self.mask_image, self.selection is not None
            )
            if mode == 1
            else self.client.post_img2img(
                self.selection_image, self.mask_image, self.selection is not None
            )
        )
        assert response is not None, "Backend Error, check terminal"

        outputs = response["outputs"]
        print(f"Getting images: {outputs}")
        layer_name_prefix = (
            "inpaint" if mode == 1 else "sd upscale" if mode == 2 else "img2img"
        )
        for i, output in enumerate(outputs):
            self.insert_img(f"{layer_name_prefix} {i + 1}", output)

        self.doc.refreshProjection()

    def apply_simple_upscale(self):
        path = os.path.join(self.cfg("sample_path", str), f"{int(time.time())}.png")
        if self.cfg("save_temp_images", bool):
            save_img(self.selection_image, path)

        response = self.client.post_upscale(self.selection_image)
        assert response is not None, "Backend Error, check terminal"
        output = response["output"]
        print(f"Getting image: {output}")

        self.insert_img(f"upscale", output)
        self.doc.refreshProjection()

    def create_mask_layer_internal(self):
        if self.selection is not None:
            self.app.action("add_new_transparency_mask").trigger()
            print(f"created mask layer")
            self.doc.setSelection(self.selection)

    def create_mask_layer_workaround(self):
        if self.cfg("create_mask_layer", bool):
            QTimer.singleShot(
                ADD_MASK_TIMEOUT,
                lambda: self.create_mask_layer_internal(),
            )

    # Actions
    def action_txt2img(self):
        self.status_changed.emit(STATE_WAIT)

        def cb():
            self.update_config()
            self.update_selection()
            self.adjust_selection()
            self.apply_txt2img()
            self.create_mask_layer_workaround()
            self.status_changed.emit(STATE_TXT2IMG)

        QTimer.singleShot(1, cb)

    def action_img2img(self):
        self.status_changed.emit(STATE_WAIT)

        def cb():
            self.update_config()
            self.update_selection()
            self.adjust_selection()
            self.apply_img2img(mode=0)
            self.create_mask_layer_workaround()
            self.status_changed.emit(STATE_IMG2IMG)

        QTimer.singleShot(1, cb)

    def action_sd_upscale(self):
        assert False, "disabled"
        self.status_changed.emit(STATE_WAIT)
        self.update_config()
        self.update_selection()
        self.apply_img2img(mode=2)
        self.create_mask_layer_workaround()

    def action_inpaint(self):
        self.status_changed.emit(STATE_WAIT)

        def cb():
            self.update_config()
            self.update_selection()
            self.adjust_selection()
            self.apply_img2img(mode=1)
            self.status_changed.emit(STATE_INPAINT)

        QTimer.singleShot(1, cb)

    def action_simple_upscale(self):
        self.status_changed.emit(STATE_WAIT)

        def cb():
            self.update_config()
            self.update_selection()
            self.apply_simple_upscale()
            self.status_changed.emit(STATE_UPSCALE)

        QTimer.singleShot(1, cb)


script = Script()
