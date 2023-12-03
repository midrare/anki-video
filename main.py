import concurrent.futures
from enum import auto
import json
import mimetypes
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import typing
import urllib.parse
import urllib.request
import uuid
import xml.dom.minidom

import anki.cards
import anki.media
import aqt
import aqt.editor
import aqt.gui_hooks
import aqt.operations
import aqt.qt
import aqt.utils

IMAGE_EXTS: typing.Final[list[str]] = [
    '.png',
    '.jpg',
    '.jpeg',
    '.tiff',
    '.gif',
    '.bmp',
    '.webp',
]
VIDEO_EXTS: typing.Final[list[str]] = [
    '.avi',
    '.mkv',
    '.webm',
    '.mp4',
]
AUDIO_EXTS: typing.Final[list[str]] = [
    '.mp3',
    '.ogg',
    '.wav',
    '.aiff',
    '.aac',
    '.wma',
    '.flac',
    '.alac',
    '.wma',
]

MEDIA_REGEXP: typing.Pattern = re.compile(
    r'anki-video-'
    + r'[a-fA-F0-9]{8}' \
    + r'-[a-fA-F0-9]{4}' \
    + r'-[a-fA-F0-9]{4}' \
    + r'-[a-fA-F0-9]{4}' \
    + r'-[a-fA-F0-9]{12}' \
    + r'\..*')

ROOT_DIR: typing.Final[pathlib.Path] = pathlib.Path(__file__).parent.absolute()
JS_FILES: typing.Final[list[pathlib.Path]] = [ROOT_DIR / "video.js"]
CSS_FILES: typing.Final[list[pathlib.Path]] = [ROOT_DIR / "video-js.css"]
ELEMENT_CLASS: typing.Final[str] = "anki-video"

THUMBNAIL_EXT: typing.Final[str] = '.png'
VIDEO_EXT: typing.Final[str] = '.webm'

assert THUMBNAIL_EXT in IMAGE_EXTS
assert VIDEO_EXT in VIDEO_EXTS

FFMPEG_EXE: typing.Final[str] = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE_EXE: typing.Final[str] = shutil.which('ffprobe') or 'ffprobe'

Config = typing.TypedDict(
    'Config', {
        "clipboard paste": bool,
        "drag-and-drop": bool,
        'autoplay': bool,
        'loop': bool,
        'controls': bool,
        'mute': bool,
        'volume': typing.Optional[float],
        'size': typing.Optional[str],
    })

DEFAULT_CONFIG: typing.Final[Config] = {
    "clipboard paste": True,
    "drag-and-drop": True,
    'autoplay': True,
    'loop': True,
    'controls': True,
    'mute': False,
    'volume': None,
    'size': None,
}

assert aqt.mw, 'no main window found'
config: Config = DEFAULT_CONFIG \
    | (aqt.mw.addonManager.getConfig(__name__) or {}) # type: ignore

_executor: concurrent.futures.Executor = concurrent.futures.ThreadPoolExecutor()
_stop_event: threading.Event = threading.Event()


def _download_video(
        url: str, dest: typing.Union[str, pathlib.Path], force: bool = False):
    if not isinstance(dest, pathlib.Path):
        dest = pathlib.Path(dest)

    if url and (force or not dest.exists()):
        remote = urllib.request.urlopen(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('wb') as f:
            f.write(remote.read())


def _exec_cmd(
    cmd: list[str],
    cancel: typing.Optional[typing.Callable[[], bool]],
) -> int:
    proc = subprocess.Popen(cmd, text=True, shell=True)
    while proc.poll() is None:
        if cancel and cancel():
            proc.kill()
            break
        try:
            time.sleep(0.1)
        except (Exception, KeyboardInterrupt) as e:
            proc.kill()
            raise e
    return proc.returncode


def _exec_ffmpeg(
    file: typing.Union[str, pathlib.Path],
    dest: typing.Union[str, pathlib.Path],
    cancel: typing.Optional[typing.Callable[[], bool]] = None,
):
    if not isinstance(file, pathlib.Path):
        file = pathlib.Path(file)
    if not isinstance(dest, pathlib.Path):
        dest = pathlib.Path(dest)

    with tempfile.TemporaryDirectory(prefix="anki-video-") as tmpdir:
        if dest.suffix.lower() in IMAGE_EXTS:
            cmd = [
                FFPROBE_EXE, '-loglevel', 'error', '-of', 'csv=p=0',
                '-show_entries', 'format=duration', file
            ]
            proc = subprocess.run(cmd, capture_output=True, shell=True)
            try:
                duration = float(proc.stdout)
            except Exception:
                duration = 0.0

            tmpfile = pathlib.Path(tmpdir, dest.name)
            cmd = [
                FFMPEG_EXE, '-loglevel', 'error', '-i', file, '-ss',
                str(duration / 2), '-update', 'true', '-vframes', '1', tmpfile
            ]
            proc = subprocess.run(cmd, shell=True)

            if proc.returncode == 0:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmpfile, dest)
        else:
            tmpfile = pathlib.Path(tmpdir, dest.name)
            cmd = [
                FFMPEG_EXE, '-loglevel', 'error', '-y', "-i", file, tmpfile
            ]
            if _exec_cmd(cmd, cancel) == 0:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmpfile, dest)


def _import_video_async(
    editor: aqt.editor.EditorWebView,
    file: typing.Union[str, pathlib.Path],
) -> tuple[str, pathlib.Path, pathlib.Path]:
    if not isinstance(file, pathlib.Path):
        file = pathlib.Path(file)

    assert aqt.mw and aqt.mw.col, 'no collection'
    media_dir = pathlib.Path(aqt.mw.col.media.dir())

    def process(
        src: typing.Union[str, pathlib.Path],
        dest: typing.Union[str, pathlib.Path],
        cancel: typing.Optional[typing.Callable[[], bool]] = None,
    ):
        if not isinstance(src, pathlib.Path):
            src = pathlib.Path(src)
        if not isinstance(dest, pathlib.Path):
            dest = pathlib.Path(dest)

        if (src.suffix.lower() == dest.suffix.lower()):
            # speedup hack
            with tempfile.TemporaryDirectory(prefix="anki-video-") as tmpdir:
                tmpfile = pathlib.Path(tmpdir, dest.name)
                shutil.copyfile(src, tmpfile)

                dest.parent.mkdir(parents=True, exist_ok=True)
                tmpfile.rename(dest)
        else:
            _exec_ffmpeg(src, dest, cancel)

    uid = str(uuid.uuid4())

    assert THUMBNAIL_EXT.startswith('.'), 'thumbnail ext must start with dot'
    assert VIDEO_EXT.startswith('.'), 'video ext must start with dot'
    thumbdest = media_dir / f"anki-video-{uid}{THUMBNAIL_EXT}"
    videodest = media_dir / f"anki-video-{uid}{VIDEO_EXT}"

    global _stop_event
    _executor.submit(process, file, thumbdest, _stop_event.is_set)
    _executor.submit(process, file, videodest, _stop_event.is_set)

    return uid, thumbdest, videodest


def _on_exit(sig: int, frame):
    global _stop_event
    _stop_event.set()


def _on_card_will_show(
    html: str,
    card: anki.cards.Card,
    context: str,
) -> str:
    html += '\n'
    html += '<!-- anki-video BEGIN -->\n'

    # css
    html += f'<style>\n'
    for css in CSS_FILES:
        with open(css, 'r') as f:
            html += f.read()
            html += '\n'
    html += '</style>\n'

    # javascript
    html += f'<script type="text/javascript">\n'
    for script in JS_FILES:
        with open(script, 'r') as f:
            html += f.read()
            html += '\n'

    autoresize = False
    width = -1
    height = -1
    if (size := config.get('size')) and isinstance(size, str):
        if (m := re.match(r'^\s*([0-9]+)(?:\s*px\s*)?' + r'[x\s,:\-/\\]+'
                          + r'([0-9]+)(?:\s*px\s*)?\s*$', size, re.IGNORECASE)):
            autoresize = False
            width = int(m.group(1))
            height = int(m.group(2))
        elif size.lower() in ['auto']:
            autoresize = True
        elif size.lower() in ['default']:
            autoresize = False

    html += f"""
        onUpdateHook.push(function() {{
            const els = document.querySelectorAll(".{ELEMENT_CLASS}");
            els.forEach((el) => {{
                var opts = {{}};
                opts.loop = {'true'
                    if config.get('loop', True) else 'false'};
                opts.mute = {'true'
                    if config.get('mute', False) else 'false'};
                opts.controls = {'true'
                    if config.get('controls', True) else 'false'};
                opts.autoplay = {'true'
                    if config.get('autoplay', True) else 'false'};
                opts.volume = {config['volume']
                    if config.get('volume') is not None else 'null'};

                if ({width} >= 0 && {height} >= 0) {{
                    opts.width = {width};
                    opts.height = {height};
                }}

                el.querySelectorAll("config").forEach((optEl) => {{
                    var key = optEl.hasAttribute("option")
                        ? optEl.getAttribute("option") : null;
                    var value = (optEl.textContent?.trim()?.length || 0) > 0
                        ? optEl.textContent.trim() : null;

                    if (key && typeof key === "string"
                    && typeof value === "string"
                    && value.toLowerCase() !== "null") {{
                        var result = parseFloat(value);

                        if (typeof result === "undefined"
                        || result === null
                        || isNaN(result)) {{
                            result = parseInt(value);
                        }}

                        if (typeof result === "undefined"
                        || result === null
                        || isNaN(result)) {{
                            result = null;
                            if (String(value).toLowerCase() === "true") {{
                                result = true;
                            }}
                            if (String(value).toLowerCase() === "false") {{
                                result = false;
                            }}
                        }}

                        if (typeof result === "undefined"
                        || result === null) {{
                            result = String(value);
                        }}

                        if (typeof result !== "undefined"
                        && result !== null
                        && result !== "null"
                        && result !== "") {{
                            opts[key] = result;
                        }}
                    }}
                }});

                var args = {{}};
                args.loop = opts.loop;
                args.muted = opts.mute;
                args.controls = opts.controls;
                args.disablePictureInPicture = true;
                args.fluid = {'true' if autoresize else 'false'};

                if (typeof opts.width === "number" && opts.width >= 0) {{
                    args.width = opts.width;
                    args.fluid = false;
                }}

                if (typeof opts.height === 'number' && opts.height >= 0) {{
                    args.height = opts.height;
                    args.fluid = false;
                }}

                globalThis.videojs(el, args, function onPlayerReady() {{
                    this.playsinline(true);

                    if (typeof opts.volume === "number" && opts.volume >= 0) {{
                        this.volume(Math.max(0.0, Math.min(1.0, opts.volume)))
                    }}

                    if (opts.autoplay) {{
                        this.play();
                    }}
                }});
            }});
        }});
        """

    html += '</script>\n'

    html += '<!-- anki-video END -->\n'
    return html


def _qurl_ext(url: aqt.qt.QUrl) -> str:
    return os.path.splitext(url.toLocalFile())[1]


def _on_editor_will_process_mime(
    mime: aqt.qt.QMimeData,
    editor: aqt.editor.EditorWebView,
    internal: bool,
    extended: bool,
    drop_event: bool,
) -> aqt.qt.QMimeData:
    if not mime.hasUrls() \
    or len(mime.urls()) != 1 \
    or any(u.scheme() != 'file' for u in mime.urls()) \
    or any(_qurl_ext(url).lower() not in VIDEO_EXTS for url in mime.urls()):
        return mime

    if (not config["drag-and-drop"] and drop_event) \
    or (not config["clipboard paste"] and not drop_event):
        return mime

    htmls = []

    for url in mime.urls():
        file = pathlib.Path(url.toLocalFile())
        uid, thumbfile, videofile = _import_video_async(editor, file)

        doc = xml.dom.minidom.Document()

        video = doc.createElement('video')
        video.setAttribute('id', uid)
        video.setAttribute('class', ' '.join([ 'video-js', ELEMENT_CLASS ]))
        video.setAttribute('controls', 'true')
        video.setAttribute('preload', 'auto')
        video.setAttribute('poster', thumbfile.name)
        doc.appendChild(video)

        # empty text child node to prevent Anki's inserthtml() mangling

        source = doc.createElement('source')
        source.setAttribute('src', videofile.name)
        source.setAttribute(
            'type',
            mimetypes.guess_type(videofile, strict=False)[0] or '')
        source.appendChild(doc.createTextNode(''))
        video.appendChild(source)

        # prevents Anki from deleting file when checking media
        # https://github.com/ankitects/anki/blob/ ...
        #   ... ae6a03942f651790c40f8d8479f90eb7715bf2af/rslib/src/text.rs#L104

        for asset in [videofile.name, thumbfile.name]:
            el = doc.createElement('object')
            el.setAttribute('hidden', 'true')
            el.setAttribute('src', asset)
            el.appendChild(doc.createTextNode(''))
            video.appendChild(el)

        for opt in [ 'autoplay', 'loop', 'controls', 'mute', 'volume']:
            el = doc.createElement('config')
            el.setAttribute('option', opt)
            el.appendChild(doc.createTextNode('null'))
            video.appendChild(el)

        htmls.append(video.toprettyxml(indent='    '))

    html = '\n'.join(htmls)
    editor.eval(
        rf"""(function () {{
        let html = {json.dumps(html)};
        if (html !== "") {{
            setFormat("inserthtml", html)
        }}
    }})();""")

    return aqt.qt.QMimeData()


def _on_editor_will_show_context_menu(
        editor: aqt.editor.EditorWebView, menu: aqt.qt.QMenu):
    menu.addAction("Edit")


def init_signals():
    signal.signal(signal.SIGINT, _on_exit)
    signal.signal(signal.SIGABRT, _on_exit)
    signal.signal(signal.SIGBREAK, _on_exit)
    signal.signal(signal.SIGTERM, _on_exit)


def init_hooks():
    aqt.gui_hooks.card_will_show.append(_on_card_will_show)
    # aqt.gui_hooks.editor_will_show_context_menu.append(
    #     _on_editor_will_show_context_menu)
    aqt.gui_hooks.editor_will_process_mime.append(_on_editor_will_process_mime)


def init_addon():
    init_signals()
    init_hooks()
