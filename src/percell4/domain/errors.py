"""Domain exception hierarchy for PerCell4.

Use these instead of bare ValueError in use cases so callers can
catch specific failure modes without string matching.
"""


class PercellError(Exception):
    """Base exception for all PerCell4 domain errors."""


class NoDatasetError(PercellError):
    """No dataset is currently loaded."""


class NoSegmentationError(PercellError):
    """No active segmentation layer is set."""


class NoMaskError(PercellError):
    """No active mask layer is set."""


class NoChannelError(PercellError):
    """No active channel is set."""


class NoCachedPhasorError(PercellError):
    """No cached phasor data exists for the requested channel.

    Raised by LoadCachedPhasor.execute when /phasor/<channel>/g is
    absent from the dataset. Callers (FlimPanel buttons, PhasorPlot
    auto-load) catch this to fall through to compute or to leave the
    phasor window empty.
    """


class CalibrationCSVError(PercellError):
    """The FLIM calibration CSV failed to parse or validate.

    Carries a list of human-readable error messages — one per failing
    row or global check — so the caller can show every problem at once
    instead of bailing on the first. ``str(error)`` joins them with
    semicolons; ``error.errors`` is the original tuple.
    """

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__("; ".join(self.errors))


class LifHeaderError(PercellError):
    """A Leica ``.lif`` container header could not be read.

    Covers both "this is not a ``.lif``" (bad block marker or separator) and
    "this ``.lif`` is damaged" (truncated payload, malformed XML). The message
    names the file and the failing check so the caller can tell those apart.
    Single-failure by nature — unlike :class:`CalibrationCSVError`, there is
    nothing to accumulate; the first bad byte ends the read.
    """


class LifCalibrationError(PercellError):
    """A ``.lif`` header parsed but its phasor calibration is unusable.

    Distinct from :class:`LifHeaderError`, which means the container itself
    could not be read. This one means the file is a valid ``.lif`` that either
    carries no per-image calibration record or carries one with unusable
    numeric fields.

    Carries a list of messages so every bad record surfaces at once, matching
    :class:`CalibrationCSVError`. ``str(error)`` joins them with semicolons;
    ``error.errors`` is the original tuple.
    """

    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__("; ".join(self.errors))


class ImportSchemeError(PercellError):
    """An in-file import scheme is malformed or unsafe.

    Raised when a scheme file has an unknown version, a malformed field, or an
    output name that would escape the output directory. The message names the
    failing field so the user can fix the scheme file by hand.
    """


class JavaUnavailableError(PercellError):
    """No working Java runtime could be found or provisioned.

    Raised when every candidate (the ``java_home`` setting, ``JAVA_HOME``, the
    PerCell cache) fails its ``java -version`` probe and provisioning is not
    possible: no consent, no network, a checksum mismatch, or a platform with
    no pinned runtime. The message says what was tried and names the manual
    steps, so it can be shown to the user verbatim.
    """


class BioformatsUnavailableError(PercellError):
    """The pinned Bio-Formats jar is missing and could not be fetched.

    Same shape as :class:`JavaUnavailableError`: the message names the
    failure (offline, checksum mismatch, non-HTTPS URL) and the manual
    alternative (set ``bioformats_jar`` in Advanced settings).
    """


class BioformatsReadError(PercellError):
    """Bio-Formats could not read the pixel data of an in-file source.

    Raised by the reader when reading planes fails: a Java exception while
    decoding, or the reader process stopping mid-read. The message is one
    line naming the file; the full trace goes to the log. Probing never
    raises this; a file that cannot be probed gets an error on its record.
    """


class ImportCancelledError(PercellError):
    """The user cancelled an in-file import before it finished.

    The importer raises this after removing its temporary file, so a
    cancelled import leaves no dataset file behind.
    """


class ProvisioningCancelledError(PercellError):
    """The user cancelled a Java or Bio-Formats download.

    Distinct from the unavailable errors so callers can treat it as a choice
    rather than a failure. By the time it is raised, no partial download or
    half-extracted runtime is left in the cache.
    """


class ProjectionRequiredError(PercellError):
    """An intensity read cannot tell which z-projection to use.

    Raised when a dataset holds several projections and none was chosen and
    none is the preferred one, when the chosen projection is not stored, or
    when the dataset holds only a z-series (it is view-only until a
    projection is added). ``stored`` lists the projections the dataset
    holds, so a caller can offer them; the message names them too.
    """

    def __init__(self, message: str, stored: tuple[str, ...] = ()) -> None:
        self.stored: tuple[str, ...] = tuple(stored)
        super().__init__(message)


class AddProjectionError(PercellError):
    """A projection cannot be added to this dataset.

    Raised when the dataset kept no z-series to project from, or already
    stores the requested projection. The message says which, so batch tools
    can report it per dataset. The dataset is left unchanged.
    """
