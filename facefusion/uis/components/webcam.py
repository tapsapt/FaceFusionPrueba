import os
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Deque, Generator, List, Optional

import cv2
import gradio
from tqdm import tqdm

from facefusion import ffmpeg_builder, logger, state_manager, wording
from facefusion.audio import create_empty_audio_frame
from facefusion.common_helper import get_first, is_windows
from facefusion.content_analyser import analyse_stream
from facefusion.face_analyser import get_average_face, get_many_faces
from facefusion.ffmpeg import open_ffmpeg
from facefusion.filesystem import filter_image_paths
from facefusion.processors.core import get_processors_modules
from facefusion.typing import Face, Fps, VisionFrame
from facefusion.uis.core import get_ui_component
from facefusion.uis.typing import StreamMode, WebcamMode
from facefusion.vision import normalize_frame_color, read_static_images, unpack_resolution

WEBCAM_CAPTURE : Optional[cv2.VideoCapture] = None
WEBCAM_IMAGE : Optional[gradio.Image] = None
WEBCAM_START_BUTTON : Optional[gradio.Button] = None
WEBCAM_STOP_BUTTON : Optional[gradio.Button] = None


def get_webcam_capture(webcam_device_id : int) -> Optional[cv2.VideoCapture]:
	global WEBCAM_CAPTURE

	if WEBCAM_CAPTURE is None:
		cv2.setLogLevel(0)
		if is_windows():
			webcam_capture = cv2.VideoCapture(webcam_device_id, cv2.CAP_DSHOW)
		else:
			webcam_capture = cv2.VideoCapture(webcam_device_id)
		cv2.setLogLevel(3)

		if webcam_capture and webcam_capture.isOpened():
			WEBCAM_CAPTURE = webcam_capture
	return WEBCAM_CAPTURE


def clear_webcam_capture() -> None:
	global WEBCAM_CAPTURE

	if WEBCAM_CAPTURE and WEBCAM_CAPTURE.isOpened():
		WEBCAM_CAPTURE.release()
	WEBCAM_CAPTURE = None


def render() -> None:
	global WEBCAM_IMAGE
	global WEBCAM_START_BUTTON
	global WEBCAM_STOP_BUTTON

	WEBCAM_IMAGE = gradio.Image(
		label = wording.get('uis.webcam_image')
	)
	WEBCAM_START_BUTTON = gradio.Button(
		value = wording.get('uis.start_button'),
		variant = 'primary',
		size = 'sm'
	)
	WEBCAM_STOP_BUTTON = gradio.Button(
		value = wording.get('uis.stop_button'),
		size = 'sm'
	)


def listen() -> None:
	webcam_device_id_dropdown = get_ui_component('webcam_device_id_dropdown')
	webcam_mode_radio = get_ui_component('webcam_mode_radio')
	webcam_resolution_dropdown = get_ui_component('webcam_resolution_dropdown')
	webcam_fps_slider = get_ui_component('webcam_fps_slider')
	source_image = get_ui_component('source_image')

	if webcam_device_id_dropdown and webcam_mode_radio and webcam_resolution_dropdown and webcam_fps_slider:
		start_event = WEBCAM_START_BUTTON.click(start, inputs = [ webcam_device_id_dropdown, webcam_mode_radio, webcam_resolution_dropdown, webcam_fps_slider ], outputs = WEBCAM_IMAGE)
		WEBCAM_STOP_BUTTON.click(stop, cancels = start_event, outputs = WEBCAM_IMAGE)

	if source_image:
		source_image.change(stop, cancels = start_event, outputs = WEBCAM_IMAGE)


def start(webcam_device_id : int, webcam_mode : WebcamMode, webcam_resolution : str, webcam_fps : Fps) -> Generator[VisionFrame, None, None]:
	state_manager.set_item('face_selector_mode', 'one')
	source_image_paths = filter_image_paths(state_manager.get_item('source_paths'))
	source_frames = read_static_images(source_image_paths)
	source_faces = get_many_faces(source_frames)
	source_face = get_average_face(source_faces)
	stream = None
	webcam_capture = None

	if webcam_mode in [ 'udp', 'v4l2' ]:
		stream = open_stream(webcam_mode, webcam_resolution, webcam_fps) #type:ignore[arg-type]
	webcam_width, webcam_height = unpack_resolution(webcam_resolution)

	if isinstance(webcam_device_id, int):
		webcam_capture = get_webcam_capture(webcam_device_id)

	if webcam_capture and webcam_capture.isOpened():
		webcam_capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG')) #type:ignore[attr-defined]
		webcam_capture.set(cv2.CAP_PROP_FRAME_WIDTH, webcam_width)
		webcam_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, webcam_height)
		webcam_capture.set(cv2.CAP_PROP_FPS, webcam_fps)

		for capture_frame in multi_process_capture(source_face, webcam_capture, webcam_fps):
			if webcam_mode == 'inline':
				yield normalize_frame_color(capture_frame)
			else:
				try:
					stream.stdin.write(capture_frame.tobytes())
				except Exception:
					clear_webcam_capture()
				yield None


def multi_process_capture(source_face : Face, webcam_capture : cv2.VideoCapture, webcam_fps : Fps) -> Generator[VisionFrame, None, None]:
	deque_capture_frames: Deque[VisionFrame] = deque()

	with tqdm(desc = wording.get('streaming'), unit = 'frame', disable = state_manager.get_item('log_level') in [ 'warn', 'error' ]) as progress:
		with ThreadPoolExecutor(max_workers = state_manager.get_item('execution_thread_count')) as executor:
			futures = []

			while webcam_capture and webcam_capture.isOpened():
				_, capture_frame = webcam_capture.read()
				if analyse_stream(capture_frame, webcam_fps):
					yield None
				future = executor.submit(process_stream_frame, source_face, capture_frame)
				futures.append(future)

				for future_done in [ future for future in futures if future.done() ]:
					capture_frame = future_done.result()
					deque_capture_frames.append(capture_frame)
					futures.remove(future_done)

				while deque_capture_frames:
					progress.update()
					yield deque_capture_frames.popleft()


def stop() -> gradio.Image:
	clear_webcam_capture()
	return gradio.Image(value = None)


def process_stream_frame(source_face : Face, target_vision_frame : VisionFrame) -> VisionFrame:
	source_audio_frame = create_empty_audio_frame()

	for processor_module in get_processors_modules(state_manager.get_item('processors')):
		logger.disable()
		if processor_module.pre_process('stream'):
			target_vision_frame = processor_module.process_frame(
			{
				'source_face': source_face,
				'source_audio_frame': source_audio_frame,
				'target_vision_frame': target_vision_frame
			})
		logger.enable()
	return target_vision_frame


def open_stream(stream_mode : StreamMode, stream_resolution : str, stream_fps : Fps) -> subprocess.Popen[bytes]:
	commands = ffmpeg_builder.chain(
		[ '-f', 'rawvideo' ],
		ffmpeg_builder.set_media_resolution(stream_resolution),
		ffmpeg_builder.set_conditional_fps(stream_fps),
		ffmpeg_builder.set_pixel_format('bgr24'),
		ffmpeg_builder.capture_stream()
	)

	if stream_mode == 'udp':
		commands.extend([ '-b:v', '2000k' ])
		commands.extend([ '-f', 'mpegts' ])
		commands.extend(ffmpeg_builder.set_output('udp://localhost:27000?pkt_size=1316')

	if stream_mode == 'v4l2':
		try:
			device_name = get_first(os.listdir('/sys/devices/virtual/video4linux'))
			if device_name:
				device_path = '/dev/' + device_name
				commands.extend([ '-f', 'v4l2' ])
				commands.extend(ffmpeg_builder.set_output(device_path))
		except FileNotFoundError:
			logger.error(wording.get('stream_not_loaded').format(stream_mode = stream_mode), __name__)

	return open_ffmpeg(commands)


def get_available_webcam_ids(webcam_id_start : int, webcam_id_end : int) -> List[int]:
	available_webcam_ids = []

	for index in range(webcam_id_start, webcam_id_end):
		webcam_capture = get_webcam_capture(index)

		if webcam_capture and webcam_capture.isOpened():
			available_webcam_ids.append(index)
			clear_webcam_capture()

	return available_webcam_ids
