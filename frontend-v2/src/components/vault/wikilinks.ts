/**
 * [[task-<uuid>]] — written by the deliverable wrapper
 * (backend/app/services/deliverable_wrapper.py). It names a task, not a
 * note: the reading panel links it to the task page and list previews show
 * the short id.
 */
export const TASK_WIKILINK_RE = /^task-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;
