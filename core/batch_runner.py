
"""Launch the SQLite run service independently of the Streamlit script thread."""

import threading

from core.run_service import run_service_instance


class BatchRunner:
    def __init__(self, service=run_service_instance):
        self.service = service
        self._lock = threading.Lock()
        self._threads = {}
        self._errors = {}

    def start_batch(self, run_id, recovery_code):
        def run():
            try:
                self.service.start_or_resume_run(run_id, recovery_code, background=False)
            except (RuntimeError, ValueError, PermissionError) as error:
                with self._lock:
                    self._errors[run_id] = str(error)
            except Exception:
                with self._lock:
                    self._errors[run_id] = "The background run could not start."
            finally:
                with self._lock:
                    self._threads.pop(run_id, None)

        with self._lock:
            if run_id in self._threads:
                return
            self._errors.pop(run_id, None)
            thread = threading.Thread(target=run, name=f"batch_{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()

    def is_running(self, run_id):
        with self._lock:
            return run_id in self._threads

    def get_error(self, run_id):
        with self._lock:
            return self._errors.pop(run_id, None)


batch_runner = BatchRunner()
