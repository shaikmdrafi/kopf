"""
A in-memory storage of arbitrary information per resource/object.

The information is stored strictly in-memory and is not persistent.
On the operator restart, all the memories are lost.

It is used internally to track allocated system resources for each Kubernetes
object, even if that object does not show up in the event streams for long time.
"""
import asyncio
import dataclasses
import logging
import time
from typing import MutableMapping, Dict, Set, Any, Iterator, Optional, Union, NewType, TYPE_CHECKING

from kopf.structs import bodies
from kopf.structs import handlers
from kopf.structs import primitives


if TYPE_CHECKING:
    asyncio_Task = asyncio.Task[None]
    asyncio_Future = asyncio.Future[Any]
else:
    asyncio_Task = asyncio.Task
    asyncio_Future = asyncio.Future

DaemonId = NewType('DaemonId', str)


@dataclasses.dataclass(frozen=True)
class Daemon:
    task: asyncio_Task  # a guarding task of the daemon.
    logger: Union[logging.Logger, logging.LoggerAdapter]
    handler: handlers.ResourceSpawningHandler
    stopper: primitives.DaemonStopper  # a signaller for the termination and its reason.


class Memo(Dict[Any, Any]):
    """ A container to hold arbitrary keys-fields assigned by the users. """

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __delattr__(self, key: str) -> None:
        try:
            del self[key]
        except KeyError as e:
            raise AttributeError(str(e))

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(str(e))


@dataclasses.dataclass(frozen=False)
class ResourceMemory:
    """ A system memo about a single resource/object. Usually stored in `Memories`. """

    # For arbitrary user data to be stored in memory, passed as `memo` to all the handlers.
    memo: Memo = dataclasses.field(default_factory=Memo)

    # For resuming handlers tracking and deciding on should they be called or not.
    noticed_by_listing: bool = False
    fully_handled_once: bool = False

    # For background and timed threads/tasks (invoked with the kwargs of the last-seen body).
    live_fresh_body: Optional[bodies.Body] = None
    idle_reset_time: float = dataclasses.field(default_factory=time.monotonic)
    forever_stopped: Set[handlers.HandlerId] = dataclasses.field(default_factory=set)
    daemons: Dict[DaemonId, Daemon] = dataclasses.field(default_factory=dict)


class ResourceMemories:
    """
    A container of all memos about every existing resource in a single operator.

    Distinct operator tasks have their own memory containers, which
    do not overlap. This solves the problem if storing the per-resource
    entries in the global or context variables.

    The memos can store anything the resource handlers need to persist within
    a single process/operator lifetime, but not persisted on the resource.
    For example, the runtime system resources: flags, threads, tasks, etc.
    Or the scalar values, which have meaning only for this operator process.

    The container is relatively async-safe: one individual resource is always
    handled sequentially, never in parallel with itself (different resources
    are handled in parallel through), so the same key will not be added/deleted
    in the background during the operation, so the locking is not needed.
    """
    _items: MutableMapping[str, ResourceMemory]

    def __init__(self) -> None:
        super().__init__()
        self._items = {}

    def iter_all_memories(self) -> Iterator[ResourceMemory]:
        for memory in self._items.values():
            yield memory

    async def recall(
            self,
            raw_body: bodies.RawBody,
            *,
            noticed_by_listing: bool = False,
    ) -> ResourceMemory:
        """
        Either find a resource's memory, or create and remember a new one.

        Keep the last-seen body up to date for all the handlers.
        """
        key = self._build_key(raw_body)
        if key not in self._items:
            memory = ResourceMemory(noticed_by_listing=noticed_by_listing)
            self._items[key] = memory
        return self._items[key]

    async def forget(
            self,
            raw_body: bodies.RawBody,
    ) -> None:
        """
        Forget the resource's memory if it exists; or ignore if it does not.
        """
        key = self._build_key(raw_body)
        if key in self._items:
            del self._items[key]

    def _build_key(
            self,
            raw_body: bodies.RawBody,
    ) -> str:
        """
        Construct an immutable persistent key of a resource.

        Generally, a uid is sufficient, as it is unique within the cluster.
        But it can be e.g. plural/namespace/name triplet, or anything else,
        even of different types (as long as it satisfies the type checkers).

        But it must be consistent within a single process lifetime.
        """
        return raw_body.get('metadata', {}).get('uid') or ''
