import os
from ffmpy import FFmpeg
import cv2
import subprocess
import json
import logging
from pathlib import Path
import ffmpeg
import dlib
import numpy as np
from collections import deque
from tqdm import tqdm
from skimage import transform
import warnings
from tqdm.contrib.concurrent import process_map

logger = logging.getLogger(__name__)

# VIDEOS_CACHE = {}
MAX_MISSING_FRAMES_RATIO = 0.75 #max video frames that is ok to be missing

def resize_frames(input_frames, new_size=(640, 480)):
    resized_frames = []
    for frame in input_frames:
        try:
            resized_frames.append(cv2.resize(frame, new_size))
        except:
            pass #some frames are corrupt or missing
    return resized_frames

def save_video(frames, out_filepath, fps, vcodec="libx264"):
    if len(frames) == 0:
        warnings.warn(
            f"Video segment `{out_filepath.stem}` has no metadata..." +
            " skipping!!" 
        )
        return
    height, width, _ = frames[0].shape
    process = (
        ffmpeg.input(
            "pipe:", format="rawvideo", pix_fmt="bgr24", s="{}x{}".format(width, height)
        )
        .output(str(out_filepath), pix_fmt="bgr24", vcodec=vcodec, r=fps)
        .overwrite_output()
        .run_async(pipe_stdin=True, quiet=True)
    )
    for _, frame in enumerate(frames):
        try:
            process.stdin.write(frame.astype(np.uint8).tobytes())
        except:
            print(process.stderr.read())
    process.stdin.close()
    process.wait()

def apply_transform(trans, img, std_size):
    warped = transform.warp(img, inverse_map=trans.inverse, output_shape=std_size)
    warped = warped * 255  # note output from warp is double image (value range [0,1])
    warped = warped.astype("uint8")
    return warped

def cut_patch(img, metadata, height, width, threshold=5):
    center_x, center_y = np.mean(metadata, axis=0)
    if center_y - height < 0:
        center_y = height
    if center_y - height < 0 - threshold:
        raise Exception("too much bias in height")
    if center_x - width < 0:
        center_x = width
    if center_x - width < 0 - threshold:
        raise Exception("too much bias in width")

    if center_y + height > img.shape[0]:
        center_y = img.shape[0] - height
    if center_y + height > img.shape[0] + threshold:
        raise Exception("too much bias in height")
    if center_x + width > img.shape[1]:
        center_x = img.shape[1] - width
    if center_x + width > img.shape[1] + threshold:
        raise Exception("too much bias in width")

    cutted_img = np.copy(
        img[
            int(round(center_y) - round(height)) : int(round(center_y) + round(height)),
            int(round(center_x) - round(width)) : int(round(center_x) + round(width)),
        ]
    )
    return cutted_img

def warp_img(src, dst, img, std_size):
    tform = transform.estimate_transform(
        "similarity", src, dst
    )  # find the transformation matrix
    warped = transform.warp(
        img, inverse_map=tform.inverse, output_shape=std_size
    )  # warp
    warped = warped * 255  # note output from wrap is double image (value range [0,1])
    warped = warped.astype("uint8")
    return warped, tform

def crop_patch(
    video_frames,
    num_frames,
    metadata,
    mean_face_metadata,
    std_size=(256, 256),
    window_margin=12,
    start_idx=48,
    stop_idx=68,
    crop_height=96,
    crop_width=96,
):
    """Crop mouth patch"""
    stablePntsIDs = [33, 36, 39, 42, 45]
    margin = min(num_frames, window_margin)
    q_frame, q_metadata = deque(), deque()
    sequence = []
    for frame_idx, frame in enumerate(video_frames):
        if frame_idx >= len(metadata):
            break  #! Sadly, this is necessary
        q_metadata.append(metadata[frame_idx])
        q_frame.append(frame)
        if len(q_frame) == margin:
            smoothed_metadata = np.mean(q_metadata, axis=0)
            cur_metadata = q_metadata.popleft()
            cur_frame = q_frame.popleft()
            # -- affine transformation
            trans_frame, trans = warp_img(
                smoothed_metadata[stablePntsIDs, :],
                mean_face_metadata[stablePntsIDs, :],
                cur_frame,
                std_size,
            )
            trans_metadata = trans(cur_metadata)
            # -- crop mouth patch
            sequence.append(
                cut_patch(
                    trans_frame,
                    trans_metadata[start_idx:stop_idx],
                    crop_height // 2,
                    crop_width // 2,
                )
            )

    while q_frame:
        cur_frame = q_frame.popleft()
        # -- transform frame
        trans_frame = apply_transform(trans, cur_frame, std_size)
        # -- transform metadata
        trans_metadata = trans(q_metadata.popleft())
        # -- crop mouth patch
        sequence.append(
            cut_patch(
                trans_frame,
                trans_metadata[start_idx:stop_idx],
                crop_height // 2,
                crop_width // 2,
            )
        )
    return sequence

def load_needed_models_for_lip_movement(metadata_path=Path("./model-bin")):
    detector = dlib.get_frontal_face_detector()
    predictor = dlib.shape_predictor(str(metadata_path/"shape_predictor_68_face_landmarks.dat"))
    mean_face_landmarks = np.load(metadata_path/"20words_mean_face.npy")
    return (
        detector, predictor, mean_face_landmarks
    )

def get_video_resolution(video_filepath):
    for stream in ffmpeg.probe(video_filepath)["streams"]:
        if stream["codec_type"] == "video":
            height = int(stream["height"])
            width = int(stream["width"])
            return height, width
    raise TypeError(f"Input file: {video_filepath} doesn't have video stream!")

def split_video_to_frames(video_filepath, fstart=None, fend=None, out_fps=25):
    # src: https://github.com/kylemcdonald/python-utils/blob/master/ffmpeg.py
    #NOTE: splitting video into frames is faster on CPU than GPU
    width, height = get_video_resolution(video_filepath)
    video_stream = ffmpeg.input(str(video_filepath)).video.filter("fps", fps=out_fps)
    channels = 3
    try:
        if fstart is not None and fend is not None:
            process = (
                video_stream.trim(start_frame=fstart, end_frame=fend)
                .setpts("PTS-STARTPTS")
                .output("pipe:", format="rawvideo", pix_fmt="bgr24")
                .run_async(pipe_stdout=True, quiet=True)
            )
            frames_counter = 0
            while frames_counter < fend - fstart:
                in_bytes = process.stdout.read(width * height * channels)
                in_frame = np.frombuffer(in_bytes, np.uint8).reshape(
                    width, height, channels
                )
                yield in_frame
                frames_counter += 1
        else:
            process = (
                video_stream.setpts("PTS-STARTPTS")
                .output("pipe:", format="rawvideo", pix_fmt="bgr24")
                .run_async(pipe_stdout=True, quiet=True)
            )
            while True:
                in_bytes = process.stdout.read(width * height * channels)
                if not in_bytes:
                    break
                in_frame = np.frombuffer(in_bytes, np.uint8).reshape(
                    width, height, channels
                )
                yield in_frame

    finally:
        process.stdout.close()
        process.wait()

def detect_landmark(image):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    # print(image.shape, gray.shape)
    rects = DETECTOR(gray, 1)
    coords = None
    for (_, rect) in enumerate(rects):
        shape = PREDICTOR(gray, rect)
        coords = np.zeros((68, 2), dtype=np.int32)
        for i in range(0, 68):
            coords[i] = (shape.part(i).x, shape.part(i).y)
    return coords

def linear_interpolate(landmarks, start_idx, stop_idx):
    start_landmarks = landmarks[start_idx]
    stop_landmarks = landmarks[stop_idx]
    delta = stop_landmarks - start_landmarks
    for idx in range(1, stop_idx - start_idx):
        landmarks[start_idx + idx] = (
            start_landmarks + idx / float(stop_idx - start_idx) * delta
        )
    return landmarks

def landmarks_interpolate(landmarks):
    """Interpolate landmarks
    param list landmarks: landmarks detected in raw videos
    """
    valid_frames_idx = [idx for idx, _ in enumerate(landmarks) if _ is not None ]
    if not valid_frames_idx:
        return None
    for idx in range(1, len(valid_frames_idx)):
        if valid_frames_idx[idx] - valid_frames_idx[idx - 1] == 1:
            continue
        else:
            landmarks = linear_interpolate(
                landmarks, valid_frames_idx[idx - 1], valid_frames_idx[idx]
            )
    valid_frames_idx = [idx for idx, _ in enumerate(landmarks) if _ is not None]
    # -- Corner case: keep frames at the beginning or at the end failed to be detected.
    if valid_frames_idx:
        landmarks[: valid_frames_idx[0]] = [
            landmarks[valid_frames_idx[0]]
        ] * valid_frames_idx[0]
        landmarks[valid_frames_idx[-1] :] = [landmarks[valid_frames_idx[-1]]] * (
            len(landmarks) - valid_frames_idx[-1]
        )
    valid_frames_idx = [idx for idx, _ in enumerate(landmarks) if _ is not None]
    assert len(valid_frames_idx) == len(landmarks), "not every frame has landmark"
    return landmarks

def extract_lip_movement(
        webcam_video,
        in_video_filepath,
        out_lip_filepath,
        num_workers=10,
    ):
    # change video framerate to 25 and lower resolution for faster processing
    print("Adjust video framerate to 25")
    
    FFmpeg(
        inputs={webcam_video: None},
        outputs={in_video_filepath: "-v quiet -filter:v fps=fps=25 -vf scale=640:480 -y"},
    ).run()
    # convert video to a list of frames
    print("Converting video into frames")
    frames = list(split_video_to_frames(in_video_filepath))
    
    # Get face landmarks from video 
    print("Extract face landmarks from video frames")
    # landmarks = [
    #     detect_landmark(frame)
    #     for frame in tqdm(frames, desc="Detecting Lip Movement")
    # ]
    landmarks = process_map(
        detect_landmark,
        frames,
        max_workers=num_workers,
        desc="Detecting Lip Movement"
    )
    invalid_landmarks_ratio = sum(lnd is None for lnd in landmarks) / len(landmarks)
    print(f"Current invalid frame ratio ({invalid_landmarks_ratio}) ")
    if invalid_landmarks_ratio > MAX_MISSING_FRAMES_RATIO:
        logging.info(
            "Invalid frame ratio exceeded maximum allowed ratio!! " +
            "Starting resizing the recorded video!!"
        )
        sequence = resize_frames(frames)
    else:
        # interpolate frames not being detected (if found).
        if invalid_landmarks_ratio != 0:
            print("Linearly-interpolate invalid landmarks")
            continuous_landmarks = landmarks_interpolate(landmarks)
        else:
            continuous_landmarks = landmarks
        # crop mouth regions
        print("Cropping the mouth region.")
        sequence = crop_patch(
            frames,
            len(frames),
            continuous_landmarks,
            MEAN_FACE_LANDMARKS,
        )
    # return lip-movement frames
    save_video(sequence, out_lip_filepath, fps=25)

def get_video_resolution_for_padding(video_path):
    """Get the resolution of the input video."""
    command = [
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=width,height', '-of', 'json', video_path
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    video_info = json.loads(result.stdout)
    width = video_info['streams'][0]['width']
    height = video_info['streams'][0]['height']
    return width, height

def calculate_padding(width, height, target_width, target_height):
    """Calculate the necessary padding to fit the video into the target resolution."""
    aspect_ratio = width / height
    target_aspect_ratio = target_width / target_height

    if aspect_ratio > target_aspect_ratio:
        # Video is wider than the target, adjust height
        new_width = target_width
        new_height = int(target_width / aspect_ratio)
        pad_top = (target_height - new_height) // 2
        pad_bottom = target_height - new_height - pad_top
        pad_left = pad_right = 0
    else:
        # Video is taller than the target, adjust width
        new_height = target_height
        new_width = int(target_height * aspect_ratio)
        pad_left = (target_width - new_width) // 2
        pad_right = target_width - new_width - pad_left
        pad_top = pad_bottom = 0

    return new_width, new_height, pad_left, pad_right, pad_top, pad_bottom

def pad_video(input_path, output_path, target_width=640, target_height=480):
    """Pad the video to ensure it fits"""
    width, height = get_video_resolution_for_padding(input_path)
    
    print("Original video resolution:", width, height)
    new_width, new_height, pad_left, pad_right, pad_top, pad_bottom = calculate_padding(width, height, target_width=target_width, target_height=target_height)

    # Create padding command
    command = [
        'ffmpeg', '-i', input_path, '-vf',
        f"scale={new_width}:{new_height},pad={target_width}:{target_height}:{pad_left}:{pad_top}:black",
        '-c:a', 'copy', output_path, '-v', 'quiet', '-y'
    ]

    # Run ffmpeg command
    subprocess.run(command)
    print("Padded video resolution:", new_width, new_height)

if __name__ == "__main__":
    DETECTOR, PREDICTOR, MEAN_FACE_LANDMARKS = load_needed_models_for_lip_movement()

    outpath = Path("./example")
    input_video_path = outpath / "raw_video.mp4"
    audio_filepath = outpath / "audio.wav"
    norm_video_filepath = outpath / "normalized_video.mp4"
    video_filepath = outpath / "video.mp4"
    noisy_audio_filepath = outpath / "noisy_audio.wav"
    lip_video_filepath = outpath / "lip_movement.mp4"
    noisy_lip_filepath = outpath / "noisy_lip_movement.mp4"

    pad_video(input_video_path, norm_video_filepath)
    
    # start the lip movement preprocessing pipeline
    extract_lip_movement(
        norm_video_filepath, video_filepath, lip_video_filepath,
        num_workers=min(os.cpu_count(), 5)
    )

    # extract audio from the video
    FFmpeg(
        inputs={norm_video_filepath: None},
        outputs={noisy_audio_filepath: "-v quiet -vn -acodec pcm_s16le -ar 16000 -ac 1 -y"},
    ).run()

    # combine audio with lip movement
    FFmpeg(
        inputs={noisy_audio_filepath: None, lip_video_filepath: None},
        outputs={noisy_lip_filepath: "-v quiet -c:v copy -c:a aac -y"},
    ).run()