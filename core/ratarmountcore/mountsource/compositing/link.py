import itertools
import os
import os.path
import stat
import sys
from abc import abstractmethod
from dataclasses import dataclass, field
from functools import reduce
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ratarmountcore.mountsource import FileInfo, MountSource
from ratarmountcore.utils import cached_property, overrides
from typing_extensions import Final, Self, final


@final
@dataclass
class _BranchPath:
    """
    An versioned branch path bound to a union path.
    """

    path: Final[str]
    """
    The absolute path in the underlying mount source.
    """

    @property
    def version(self):
        return self.unionPath.version

    parent: Final[Optional["_BranchPath"]]
    """
    The parent folder version, or None if the current path is '/'.

    This establishes a "physical" parent chain that is used for relative parent path resolution (e.g., for '..'). This behavior is analogous to **lexical scoping** in programming languages, where the meaning of a relative path is determined by its static location in the filesystem hierarchy, not by how
    it is accessed.
    """

    unionPath: Final["_UnionPath"]
    """
    The union path to which this file version belongs.
    """

    def list_child_names(self) -> Optional[Iterable[str]]:
        branch_list = self.unionPath.root.layer.mountSource.list_mode(self.path)
        if branch_list is None:
            return None
        if isinstance(branch_list, Mapping):
            return branch_list.keys()
        assert isinstance(branch_list, Iterable)
        return branch_list

    @cached_property
    def file_info(self) -> FileInfo:
        """The FileInfo for this file version."""
        return self.unionPath.root.layer.mountSource.lookup(
            self.path, fileVersion=self.version
        )

    @cached_property
    def link_target(self) -> Optional["_UnionPath"]:
        """
        Resolves the link if this is a symlink or hardlink that should be resolved, otherwise returns None.
        """
        if self.file_info.linkname:
            normalizedLinkname = os.path.normpath(self.file_info.linkname)
            if self.unionPath.root.layer.shouldResolveLink(
                normalizedLinkname, stat.S_IFMT(self.file_info.mode)
            ):
                if os.path.isabs(normalizedLinkname):
                    return self.unionPath.root._lookup_absolute_path(normalizedLinkname)
                if self.parent is None:
                    raise FileNotFoundError(
                        f"Cannot resolve relative link {normalizedLinkname} from root"
                    )
                parts = normalizedLinkname.split(os.path.sep)
                partGroups = itertools.groupby(
                    parts, lambda part: part == os.path.pardir
                )
                isPart0Pardirs, partGroup0 = next(partGroups)
                if isPart0Pardirs:

                    def resolve_parent(
                        parent: _BranchPath, part: str
                    ) -> Optional[_BranchPath]:
                        if parent is None:
                            raise FileNotFoundError(
                                f"Cannot resolve parent for {normalizedLinkname} from {self.path}"
                            )
                        assert part == os.path.pardir
                        return parent.parent

                    resolvedParent = reduce(resolve_parent, partGroup0, self.parent)
                    if resolvedParent is None:
                        raise FileNotFoundError(
                            f"Cannot resolve outer parent when resolving {normalizedLinkname} from {self.path}"
                        )
                    isPart1Pardirs, normalParts = next(partGroups, (False, ()))
                    assert not isPart1Pardirs
                else:
                    resolvedParent = self.parent
                    normalParts = partGroup0
                resolvedUnionPath = reduce(
                    lambda parent, part: parent.lookup_child(part),
                    normalParts,
                    resolvedParent.unionPath,
                )
                nextPartGroup = next(partGroups, None)
                assert (
                    nextPartGroup is None
                ), "Unexpected part group after normal parts in link resolution"

                return resolvedUnionPath

            return None
        return None


@dataclass
class _UnionPath:
    """
    Represents a path in the union filesystem, which may correspond to multiple
    branch file versions.
    """

    @cached_property
    @abstractmethod
    def root(self) -> "_RootUnionPath":
        """The root union path of this union filesystem."""
        ...

    @cached_property
    @abstractmethod
    def path(self) -> str:
        """The absolute path of this union path."""
        ...

    @cached_property
    @abstractmethod
    def version(self) -> str:
        """
        The version number of the file in the underlying mount source.
        """
        ...

    @cached_property
    @abstractmethod
    def branches(self) -> Iterable[_BranchPath]:
        """The file versions that constitute this union path."""
        ...

    def generate_direct_link_targets(self):
        """Generates the direct link targets of the file versions in this union path."""
        for branchFile in self.branches:
            if branchFile.link_target is not None:
                yield branchFile.link_target

    @cached_property
    def deduplicated_transitive_link_targets(self) -> Iterable["_UnionPath"]:
        """
        Returns all transitive link targets of this union path, deduplicated.
        """
        visited: Dict[str, _UnionPath] = {}

        def visit(unionPath: _UnionPath):
            if unionPath.path not in visited:
                visited[unionPath.path] = unionPath
                for branchFile in unionPath.branches:
                    if branchFile.link_target is not None:
                        visit(branchFile.link_target)

        visit(self)
        return visited.values()

    def generate_own_branches(self) -> Iterator[_BranchPath]:
        """Generates branches that are directly part of this union path, not a result of a link."""
        for branch in self.branches:
            if branch.link_target is None:
                yield branch

    def generate_resolved_branches(self) -> Iterator[_BranchPath]:
        """Generates all branches for this path, including resolved links."""
        for linkTarget in self.deduplicated_transitive_link_targets:
            yield from linkTarget.generate_own_branches()

    @cached_property
    def resolved_branches(self):
        return tuple(self.generate_resolved_branches())

    @cached_property
    def resolved_folder_branches(self) -> Sequence[_BranchPath]:
        """Returns all resolved folder versions for this path."""
        return tuple(
            branch
            for branch in self.resolved_branches
            if stat.S_ISDIR(branch.file_info.mode)
        )

    # @cached_property
    # def resolved_nonfolder_branches(self) -> Sequence[_BranchPath]:
    #     """Returns all resolved non-folder versions for this path."""
    #     return tuple(
    #         versionedPath
    #         for versionedPath in self.generate_resolved_branches()
    #         if not stat.S_ISDIR(versionedPath.file_info.mode)
    #     )

    def lookup_child(self, name: str) -> "_ChildUnionPath":
        """Looks up a child of this union path.

        This lookup operates on the logical, merged view of the filesystem. It searches for the child `name` within all concrete folder versions that constitute this `_UnionPath`. This behavior is analogous to **dynamic dispatch** in object-oriented programming, where the operation is dispatched to the concrete implementations at runtime instead of being statically bound.
        """
        return _ChildUnionPath(
            parent=self,
            name=name,
        )


@final
@dataclass
class _ChildUnionPath(_UnionPath):
    """Represents a non-root path in the union filesystem."""

    name: Final[str]
    """The name of this path segment."""
    parent: Final[_UnionPath]
    """The parent union path."""

    @cached_property
    def version(self):
        return self.parent.version

    @cached_property
    def path(self) -> str:
        """The absolute path of this union path."""
        return os.path.join(
            self.parent.path,
            self.name,
        )

    @cached_property
    def branches(self):
        """The file versions that constitute this union path."""
        return tuple(
            _BranchPath(
                path=os.path.join(parentBranch.path, self.name),
                parent=parentBranch,
                unionPath=self,
            )
            for parentBranch in self.parent.resolved_folder_branches
        )

    @cached_property
    def root(self) -> "_RootUnionPath":
        """Returns the root union path of this union filesystem."""
        return self.parent.root


@final
@dataclass
class _RootUnionPath(_UnionPath):
    """Represents the root path in the union filesystem."""

    layer: Final["LinkResolutionLayer"]
    """The LinkResolutionLayer this path belongs to."""

    @cached_property
    def root(self) -> "_RootUnionPath":
        """Returns the root union path of this union filesystem."""
        return self

    @cached_property
    def version(self):
        return self.underlyingVersion

    underlyingVersion: Final[int]

    @cached_property
    def path(self) -> str:
        """The absolute path of this union path."""
        return "/"

    @cached_property
    def branches(self) -> Iterable[_BranchPath]:
        """The file versions that constitute this union path."""
        return (
            _BranchPath(
                path="/",
                parent=None,
                unionPath=self,
            ),
        )

    def _lookup_absolute_path(self, path: str) -> _UnionPath:
        """
        Looks up a _UnionPath for a given path string.

        Supports both absolute and relative paths. Relative paths are treated as absolute paths by prepending a "/" if needed.
        """
        parts = os.path.normpath(path).split(os.path.sep)
        return reduce(
            lambda parent, part: parent.lookup_child(part),
            itertools.dropwhile(lambda part: part == "", parts),
            self,
        )


@dataclass
class LinkResolutionLayer(MountSource):
    """
    A MountSource layer that resolves symbolic links in an branch MountSource.

    This class wraps another MountSource and provides a view where symbolic links and hard links
    are resolved. It can be configured with a `shouldResolveLink` function to
    control which links are treated as transparent links and which are kept as
    symbolic link entries or hard link entries.
    """

    mountSource: Final[MountSource]
    """The branch MountSource to resolve links in."""
    shouldResolveLink: Final[Callable[[str, int], bool]]
    """A function that determines whether a given link should be resolved.

    Args:
        linkname (str): The link target string.
        file_type (int): The file type of the link, as returned by `stat.S_IFMT(mode)`.
    """

    @overrides(MountSource)
    def versions(self, path: str) -> int:
        """
        Returns the number of available versions for a given path, after link resolution.
        """
        return sum(
            len(
                _RootUnionPath(layer=self, underlyingVersion=underlyingVersion)
                ._lookup_absolute_path(path)
                .resolved_branches
            )
            for underlyingVersion in range(self.mountSource.versions("/"))
        )

    @overrides(MountSource)
    def lookup(self, path: str, fileVersion: int = 0) -> Optional[FileInfo]:
        """
        Looks up file information for a given path and version, after link resolution.
        """
        if fileVersion >= 0:
            try:
                (resolvedBranch,) = itertools.islice(
                    (
                        resolvedBranch
                        for underlyingVersion in range(self.mountSource.versions("/"))
                        for resolvedBranch in _RootUnionPath(
                            layer=self, underlyingVersion=underlyingVersion
                        )
                        ._lookup_absolute_path(path)
                        .resolved_branches
                    ),
                    fileVersion,
                    fileVersion + 1,
                )
            except ValueError:
                return None
            else:
                return resolvedBranch.file_info
        else:
            try:
                (resolvedBranch,) = itertools.islice(
                    (
                        resolvedBranch
                        for underlyingVersion in range(
                            -1, -1 - self.mountSource.versions("/")
                        )
                        for resolvedBranch in reversed(
                            _RootUnionPath(
                                layer=self, underlyingVersion=underlyingVersion
                            )
                            ._lookup_absolute_path(path)
                            .resolved_branches
                        )
                    ),
                    -fileVersion - 1,
                    -fileVersion,
                )
            except ValueError:
                return None
            else:
                return resolvedBranch.file_info

    def _list(self, path: str) -> Optional[Iterable[str]]:
        unionPath = _RootUnionPath(
            layer=self, underlyingVersion=0
        )._lookup_absolute_path(path)
        if unionPath.resolved_folder_branches:
            return {
                childName
                for versionedPath in unionPath.resolved_folder_branches
                for childName in versionedPath.list_child_names()
            }
        return None

    @overrides(MountSource)
    def list(self, path: str) -> Optional[Union[Iterable[str], Dict[str, FileInfo]]]:
        """
        Lists the contents of a directory, after link resolution.
        """
        return self._list(path)

    @overrides(MountSource)
    def list_mode(self, path: str) -> Optional[Union[Iterable[str], Dict[str, int]]]:
        """
        Lists the contents of a directory with file modes, after link resolution.
        """
        return self._list(path)

    @overrides(MountSource)
    def open(self, fileInfo: FileInfo, buffering=-1) -> IO[bytes]:
        """
        Opens a file for reading, after link resolution.
        """
        unionPath = fileInfo.userdata.pop()
        try:
            return self.mountSource.open(fileInfo, buffering)
        finally:
            fileInfo.userdata.append(unionPath)

    @overrides(MountSource)
    def read(self, fileInfo: FileInfo, size: int, offset: int) -> bytes:
        """
        Reads data from a file, after link resolution.
        """
        unionPath = fileInfo.userdata.pop()
        try:
            return self.mountSource.read(fileInfo, size, offset)
        finally:
            fileInfo.userdata.append(unionPath)

    @overrides(MountSource)
    def list_xattr(self, fileInfo: FileInfo) -> List[str]:
        """
        Lists extended attributes of a file, after link resolution.
        """
        unionPath = fileInfo.userdata.pop()
        try:
            return self.mountSource.list_xattr(fileInfo)
        finally:
            fileInfo.userdata.append(unionPath)

    @overrides(MountSource)
    def get_xattr(self, fileInfo: FileInfo, key: str) -> Optional[bytes]:
        """
        Gets an extended attribute of a file, after link resolution.
        """
        unionPath = fileInfo.userdata.pop()
        try:
            return self.mountSource.get_xattr(fileInfo, key)
        finally:
            fileInfo.userdata.append(unionPath)

    @overrides(MountSource)
    def is_immutable(self) -> bool:
        """
        Returns whether the underlying mount source is immutable.
        """
        return self.mountSource.is_immutable()

    @overrides(MountSource)
    def exists(self, path: str) -> bool:
        """
        Checks if a path exists, after link resolution.
        """
        unionPath = _RootUnionPath(
            layer=self, underlyingVersion=0
        )._lookup_absolute_path(path)
        return bool(unionPath.resolved_branches)

    @overrides(MountSource)
    def is_dir(self, path: str) -> bool:
        """
        Checks if a path is a directory, after link resolution.
        """
        unionPath = _RootUnionPath(
            layer=self, underlyingVersion=0
        )._lookup_absolute_path(path)
        return bool(unionPath.resolved_folder_branches)

    @overrides(MountSource)
    def get_mount_source(self, fileInfo: FileInfo) -> Tuple[str, MountSource, FileInfo]:
        """
        Gets the mount source for a file, after link resolution.
        """
        sourceFileInfo = fileInfo.clone()
        unionPath = sourceFileInfo.userdata.pop()
        assert isinstance(unionPath, _UnionPath)
        return self.mountSource.get_mount_source(sourceFileInfo)

    @overrides(MountSource)
    def statfs(self) -> Dict[str, Any]:
        """
        Returns filesystem statistics.
        """
        return self.mountSource.statfs()

    @overrides(MountSource)
    def __exit__(self, exception_type, exception_value, exception_traceback):
        """
        Cleanup method for the mount source.
        """
        return super().__exit__(
            exception_type, exception_value, exception_traceback
        ) or self.mountSource.__exit__(
            exception_type, exception_value, exception_traceback
        )

    @overrides(MountSource)
    def __enter__(self) -> Self:
        """
        Context manager entry point for the mount source.
        """
        self.mountSource.__enter__()
        return super().__enter__()
