import time
from functools import partial
import inspect
from dataclasses import dataclass
from typing import Callable, Optional, List
import bpy
from bpy.types import Operator, Context

INTERVAL = 0.1


def ascii_progress(step, total, width=60):
    frac = step / total
    filled = int(width * frac)
    bars = "█" * filled + "░" * (width - filled)
    return f"[{bars}] {frac:.0%}"


@dataclass
class PipelineTask:
    start: Callable
    poll: Optional[Callable] = None
    deferred: bool = True
    timelog: bool = False


class PipelineOperator(Operator):
    """Safe long-running pipeline operator"""
    bl_idname = "wm.pipeline_operator"
    bl_label = "Pipeline Operator"

    _timer = None
    _tasks: List[PipelineTask] = []
    _step: int = 0
    _running: bool = False
    _deferred_done: bool = False
    _response: dict
    _start_time = 0

    cancellable = True

    # -----------------------------
    # Override this
    # -----------------------------
    def get_tasks(self) -> List[PipelineTask]:
        return []

    # -----------------------------
    # Modal loop (scheduler ONLY)
    # -----------------------------
    def modal(self, context: Context, event):
        if self.cancellable and event.type == 'ESC':
            self.finish(context, cancelled=True)
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}

        if self._step >= len(self._tasks):
            self.finish(context, cancelled=False)
            return {'FINISHED'}

        task = self._tasks[self._step]
        name = task.start.__name__.replace("_", " ").title()
        context.area.header_text_set(f"{ascii_progress(self._step, len(self._tasks))} ({name})")

        # ----------------------------------
        # Start task (once)
        # ----------------------------------
        if not self._running:
            self._running = True
            self._response = None
            self._start_time = time.time()
            task_start_with_params = partial(task.start, context) if 'context' in inspect.signature(task.start).parameters else task.start

            if task.deferred:
                self._deferred_done = False

                def wrapper():
                    self._response = task_start_with_params()
                    self._deferred_done = True
                    return None  # one-shot

                bpy.app.timers.register(wrapper, first_interval=0)
            else:
                self._response = task_start_with_params()

        # ----------------------------------
        # Wait for deferred execution
        # ----------------------------------
        if task.deferred and not self._deferred_done:
            return {'RUNNING_MODAL'}

        # ----------------------------------
        # Cancel if cancelable
        # ----------------------------------
        if self.cancellable and self._response == {'CANCELLED'}:
            self.finish(context, cancelled=True)
            return {'CANCELLED'}

        # ----------------------------------
        # Poll async tasks
        # ----------------------------------
        if task.poll:
            try:
                if task.poll():
                    return {'RUNNING_MODAL'}
            except ReferenceError:
                self.finish(context, cancelled=True)
                return {'CANCELLED'}

        # ----------------------------------
        # Task finished → advance
        # ----------------------------------
        self._running = False
        self._step += 1
        context.window_manager.progress_update(self._step + 1)
        milliseconds = (time.time() - self._start_time) * 1000
        print(f"[PIPELINE] Task \"{name}\" {' ' * (30 - len(name))} took {milliseconds:,.0f} ms")
        if task.timelog:
            self.report({"INFO"}, f"Task \"{name}\" took {milliseconds:,.0f} ms")

        return {'RUNNING_MODAL'}

    # -----------------------------
    # Execute
    # -----------------------------
    def execute(self, context: Context):
        self._tasks = self.get_tasks()
        if not self._tasks:
            self.report({'WARNING'}, "No pipeline tasks")
            return {'CANCELLED'}

        print("\n[PIPELINE BEGIN]")

        self._step = 0
        self._running = False

        wm = context.window_manager
        wm.progress_begin(0, len(self._tasks) + 1)
        wm.progress_update(1)

        self._timer = wm.event_timer_add(INTERVAL, window=context.window)
        wm.modal_handler_add(self)

        return {'RUNNING_MODAL'}

    # -----------------------------
    # Finish / cleanup
    # -----------------------------
    def finish(self, context: Context, cancelled: bool):
        wm = context.window_manager

        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None

        wm.progress_end()
        context.area.header_text_set(None)

        print("[PIPELINE END]\n")


# class PipelineOperator(Operator):
#     """Base class for long-running tasks"""
#     bl_idname = "wm.long_base_task"
#     bl_label = "Long Running Task Base"

#     context = None
#     tasks = []
#     cancellable = True
#     _timer = None
#     step_index = 0
#     max_steps = 0
#     _running = False
#     first_run = False

#     def get_tasks(self):
#         return []

#     def modal(self, context: Context, event):
#         if self.cancellable and event.type == 'ESC':
#             self.finish(context, False)
#             return {'CANCELLED'}

#         if event.type == 'TIMER':
#             if self.step_index >= len(self.tasks):
#                 self.finish(context, True)
#                 return {'FINISHED'}

#             process, still_processing = self.tasks[self.step_index]
#             process_name: str = process.__name__
#             process_name = ' '.join([word[0].upper()+word[1:] for word in process_name.split('_')])
#             context.area.header_text_set(f"{ascii_progress(self.step_index, self.max_steps + 1)} ({process_name})")

#             if not self._running:
#                 if self.first_run:
#                     self.first_run = False
#                     return {'RUNNING_MODAL'}
#                 self.first_run = True
#                 if 'context' in inspect.signature(process).parameters:
#                     with CodeTimer(f"Pipeline Process: {process_name}"):
#                         result = process(context)
#                 else:
#                     with CodeTimer(f"Pipeline Process: {process_name}"):
#                         result = process()
#                 if result == {'CANCELLED'}:
#                     self.finish(context, False)
#                     return {'CANCELLED'}
#                 self._running = True

#             finished_execution = True
#             if still_processing is not None:
#                 if 'context' in inspect.signature(still_processing).parameters:
#                     finished_execution = not still_processing(context)
#                 else:
#                     finished_execution = not still_processing()

#             if finished_execution:
#                 self.step_index += 1
#                 context.window_manager.progress_update(self.step_index + 1)
#                 context.area.header_text_set(f"{ascii_progress(self.step_index + 1, self.max_steps + 1)} ({process_name})")
#                 self._running = False

#         return {'RUNNING_MODAL'}

#     def execute(self, context: Context):
#         self.context = context
#         self.tasks = self.get_tasks()
#         if not self.tasks:
#             self.report({'WARNING'}, "No tasks to run")
#             return {'CANCELLED'}

#         self.max_steps = len(self.tasks)
#         self.step_index = 0

#         wm = context.window_manager
#         wm.progress_begin(0, self.max_steps + 1)
#         wm.progress_update(1)
#         context.area.header_text_set(ascii_progress(1, self.max_steps + 1))
#         self._timer = wm.event_timer_add(INTERVAL, window=context.window)
#         wm.modal_handler_add(self)
#         return {'RUNNING_MODAL'}

#     def finish(self, context: Context, ran_to_completion: bool):
#         wm = context.window_manager
#         if self._timer:
#             wm.event_timer_remove(self._timer)
#         wm.progress_end()
#         context.area.header_text_set(None)
#         if not ran_to_completion:
#             self.report({'WARNING'}, "Task Cancelled")


def register():
    bpy.utils.register_class(PipelineOperator)


def unregister():
    bpy.utils.unregister_class(PipelineOperator)
