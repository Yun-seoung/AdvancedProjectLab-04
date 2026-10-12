import logging
import threading

from django.conf import settings
from django.db import close_old_connections

from . import services
from .models import Meeting, Recording

logger = logging.getLogger(__name__)

_active = set()
_active_lock = threading.Lock()
_meeting_locks = {}


def _meeting_lock(meeting_id):
    with _active_lock:
        return _meeting_locks.setdefault(meeting_id, threading.Lock())


def refresh_meeting(meeting):
    recordings = list(meeting.recordings.all())
    transcribed = [r for r in recordings if r.transcript]
    if len(transcribed) > 1:
        # 녹음이 여러 개면 어느 파일에서 나온 내용인지 구분할 수 있게 파일명으로 구획을 나눈다.
        full_transcript = "\n\n".join("[%s]\n%s" % (r.filename, r.transcript) for r in transcribed)
    else:
        full_transcript = "\n\n".join(r.transcript for r in transcribed)
    meeting.transcript = full_transcript
    meeting.record = services.summarize(full_transcript) if full_transcript else ""
    if any(r.status == Recording.STATUS_PROCESSING for r in recordings):
        status = Meeting.STATUS_PROCESSING
    elif any(r.status == Recording.STATUS_DONE for r in recordings):
        status = Meeting.STATUS_DONE
    elif recordings:
        status = Meeting.STATUS_FAILED
    else:
        status = ""
    meeting.processing_status = status
    meeting.save(update_fields=["transcript", "record", "processing_status"])


def process_recording(recording_id):
    recording = Recording.objects.select_related("meeting").get(pk=recording_id)
    meeting = recording.meeting
    with _meeting_lock(meeting.id):
        try:
            recording.transcript = services.transcribe(recording.file.path) or ""
            recording.status = Recording.STATUS_DONE
            recording.error_message = ""
        except Exception as exc:
            logger.exception("recording processing failed for recording %s", recording_id)
            recording.status = Recording.STATUS_FAILED
            recording.error_message = str(exc)[:300]
        recording.save(update_fields=["transcript", "status", "error_message"])
        refresh_meeting(meeting)


def recover_stuck(meeting):
    with _active_lock:
        alive = set(_active)
    stuck = [r for r in meeting.recordings.all() if r.status == Recording.STATUS_PROCESSING and r.id not in alive]
    if not stuck:
        return
    for recording in stuck:
        recording.status = Recording.STATUS_FAILED
        recording.error_message = "서버가 재시작되어 처리가 중단되었습니다"
        recording.save(update_fields=["status", "error_message"])
    refresh_meeting(meeting)


def start_processing(recording_id):
    if not getattr(settings, "BACKGROUND_RECORDING", True):
        process_recording(recording_id)
        return

    meeting_id = Recording.objects.values_list("meeting_id", flat=True).get(pk=recording_id)
    with _active_lock:
        _active.add(recording_id)
    Meeting.objects.filter(pk=meeting_id).update(processing_status=Meeting.STATUS_PROCESSING)

    def run():
        try:
            process_recording(recording_id)
        finally:
            with _active_lock:
                _active.discard(recording_id)
            close_old_connections()

    threading.Thread(target=run, daemon=True).start()
