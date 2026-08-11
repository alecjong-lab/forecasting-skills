# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core",
#   "cf-xarray",
#   "cftime>=1.6",
#   "xarray",
#   "numpy",
# ]
# ///
"""Max consecutive-run length along a time-like dim satisfying a threshold comparison.

For each selected data variable, computes the longest run of consecutive
entries along the time dim (``time`` or ``step``) satisfying
``value <comparison> threshold`` — e.g. a dry-spell length with
``--comparison lt`` on precipitation, or a wet-spell / heatwave length with
``--comparison ge``. Data variables that don't carry the time dim pass
through untouched.
"""

import operator
import sys

from weather_skills_core import UsageError, WroteSummary, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"

_COMPARISONS = {
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}

_COMPARISON_SYMBOLS = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤"}


def _max_consecutive_run_nd(block, threshold, comp):
    """Max consecutive-run length along the last axis, vectorized over every
    leading (batch) dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis
    threshold : float, compared against block via `comp`
    comp : one of operator.gt/ge/lt/le

    Loops only over n_time (small, e.g. tens of steps) instead of over every
    gridpoint/member combination; each loop iteration is a single numpy op
    over the whole batch. Any missing value anywhere along the time axis for
    a given batch element marks that element's result NaN (a run computed
    across a data gap can't be trusted to be the true max).
    """
    import numpy as np

    condition = comp(block, threshold)
    has_nan = np.isnan(block).any(axis=-1)

    counter = np.zeros(block.shape[:-1], dtype=float)
    max_run = np.zeros(block.shape[:-1], dtype=float)
    for t in range(block.shape[-1]):
        counter = (counter + 1) * condition[..., t]
        np.maximum(max_run, counter, out=max_run)

    max_run[has_nan] = np.nan
    return max_run


def _normalize_args(args):
    # Normalize provenance args before stamping so reordered or duplicated
    # --variable flags don't cause spurious cache misses; --threshold and
    # --comparison are already scalars and need no normalization.
    if args.get("variable") is not None:
        args["variable"] = sorted(set(args["variable"]))
    return args


@weather_skill(
    "spell-length",
    _SKILL_VERSION,
    input_type="any",
    # Collapsing the time dim changes which canonical envelope shape the
    # output falls into (e.g. a forecast envelope loses its `step` axis), so
    # the union declares every zarr envelope shape; the returned dataset's
    # detected shape is validated against it before the write.
    output_type=("gridded", "forecast", "station"),
    input_paths=True,
    variable={
        "mode": "repeat",
        "help": "Restrict the computation to this data variable. Repeatable. "
        "Each selected variable must carry the time dim. Default (unset): "
        "every data variable carrying the time dim.",
    },
    time_dim=True,
    extra_args={
        "threshold": {
            "required": True,
            "type": float,
            "help": "Value to compare each time-step against, in the variable's own units.",
        },
        "comparison": {
            "required": True,
            "choices": ["gt", "ge", "lt", "le"],
            "help": "Comparison applied as value <op> threshold, e.g. 'lt' for a dry-spell "
            "(below-threshold) run, 'ge' for a wet-spell (at-or-above-threshold) run.",
        },
    },
    normalize_args=_normalize_args,
)
def spell_length(ds, input_paths, variable, time_dim, threshold, comparison):
    """Max consecutive-run length along a time-like dim satisfying a threshold comparison."""
    import numpy as np
    import xarray as xr

    src = input_paths[0]

    # Time-dim detection, mirroring aggregate-temporal's: an explicit
    # --time-dim override wins; otherwise try the CF "T" axis, preferring a
    # literal `time` dim, but fall back to `step` (forecast lead time,
    # timedelta64 - not a CF T axis) when `time` is a size-1 scalar-like
    # init-date dim alongside a `step` axis.
    if time_dim:
        dim = time_dim
        if dim not in ds.dims:
            raise UsageError(f"--time-dim '{dim}' not in dims {list(ds.dims)}")
    else:
        import cf_xarray  # noqa: F401 — registers the .cf accessor

        try:
            cf_time = ds.cf["time"].name
        except KeyError:
            cf_time = "time" if "time" in ds.dims else None
        if cf_time is not None and cf_time in ds.dims:
            if ds.sizes[cf_time] == 1 and "step" in ds.dims:
                print(
                    f"Note: time dim '{cf_time}' has size 1 alongside a "
                    f"'step' dim; computing spell length over step instead. "
                    f"Pass --time-dim {cf_time} to override.",
                    file=sys.stderr,
                )
                dim = "step"
            else:
                if "step" in ds.dims:
                    print(
                        f"Note: both '{cf_time}' and 'step' dims are present; "
                        f"computing spell length over {cf_time}. Pass "
                        f"--time-dim step to use the forecast lead axis instead.",
                        file=sys.stderr,
                    )
                dim = cf_time
        elif "step" in ds.dims:
            dim = "step"
        else:
            dim = None
        if dim is None:
            non_dim_time = cf_time
            if non_dim_time is None and "time" in ds.coords:
                non_dim_time = "time"
            if non_dim_time is not None:
                raise UsageError(
                    f"found a '{non_dim_time}' coordinate, but it is "
                    f"not a dimension of the data (a scalar coordinate has no "
                    f"axis to scan) and no 'step' dim is present. "
                    f"Dims: {list(ds.dims)}. Pass --time-dim to override."
                )
            raise UsageError(
                f"no time/step dim identified in {list(ds.dims)}. Pass --time-dim to override."
            )

    # Variable selection, mirroring `reduce`: explicit --variable names must
    # be data variables and must each carry the time dim. Default selection
    # takes every data variable carrying it; the rest pass through untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of {src}. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        missing = [v for v in selected if dim not in ds[v].dims]
        if missing:
            raise UsageError(f"variable(s) {missing} do not carry time dim '{dim}'.")
    else:
        selected = [v for v in ds.data_vars if dim in ds[v].dims]
        if not selected:
            raise UsageError(f"no data variable carries time dim '{dim}'.")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    print(
        f"Computing spell length dim={dim} comparison={comparison} "
        f"threshold={threshold} variables={selected}",
        file=sys.stderr,
    )

    comp = _COMPARISONS[comparison]
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        result = xr.apply_ufunc(
            _max_consecutive_run_nd,
            da,
            threshold,
            comp,
            input_core_dims=[[dim], [], []],
            dask="parallelized",
            dask_gufunc_kwargs={"allow_rechunk": True},
            output_dtypes=[np.float64],
        )
        src_units = da.attrs.get("units", "")
        unit_suffix = f" {src_units}" if src_units and src_units != "1" else ""
        symbol = _COMPARISON_SYMBOLS[comparison]
        label = f"{var} spell ({symbol} {threshold}{unit_suffix})"
        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source's standard_name/long_name describe the input
        # physical quantity, not this derived run-length count, and neither
        # survives the unit change. No standard_name is set — CF has no
        # entry for "consecutive spell length" to verify against. long_name
        # is set (not just GRIB_name) because `plot`'s colorbar-label
        # resolution checks long_name first. units="1" is the CF convention
        # for a dimensionless count; this equals real days only when the
        # time dim's cadence is daily — the skill counts entries, not
        # calendar duration.
        result.attrs = {
            "GRIB_name": label,
            "long_name": label,
            "units": "1",
        }
        out_ds[var] = result

    # The collapsed dim disappears from the output (with its coordinates)
    # once no data variable carries it; a dim still carried by a
    # pass-through variable stays.
    if dim in out_ds.dims and all(dim not in out_ds[v].dims for v in out_ds.data_vars):
        out_ds = out_ds.drop_dims(dim)

    return out_ds, WroteSummary(f"{out_ds.sizes}", replace=True)


if __name__ == "__main__":
    spell_length()
