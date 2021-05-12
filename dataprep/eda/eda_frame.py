"""Defines DataArray."""

from functools import reduce
from math import ceil
from typing import Any, List, Optional, Sequence, Tuple, Union, cast, Dict
from collections import Counter
import warnings

import dask
import dask.array as da
import dask.dataframe as dd
import numpy as np
import pandas as pd
import pandas._libs.missing as libmissing

from .dtypes_v2 import (
    NUMERICAL_DTYPES,
    DType,
    DTypeDef,
    detect_dtype,
    Nominal,
)

DataFrame = Union[pd.DataFrame, dd.DataFrame, "EDAFrame"]


class EDAFrame:
    """EDAFrame provides an abstraction over dask DataFrame
    and dask Array. The reason is that sometimes some algorithms
    only works on the Array and not the DataFrame. However,
    the cost for getting the array from a dask DataFrame (with known length)
    is non trivial. Instead of computing the array from a dask
    DataFrame again and again, it would be better do that once.

    Other reasons to have a separate EDAFrame abstraction includes
    converting the column names to string without modifying the
    DataFrame from user, and preprocessings like dropna and type detection.

    Parameters
    ----------
    df
        The DataFrame
    value_length
        Whether to compute the lengths of the array.
        This triggers a read on the data thus expensive if the passed in df
        is a dask DataFrame.
        If a pandas DataFrame passed in, lengths will always be compute.
    repartition
        Whether to repartition the DataFrame into 128M chunks.
    dtype: str or DType or dict of str or dict of DType, default None
        Specify Data Types for designated column or all columns.
        E.g.  dtype = {"a": Continuous, "b": "Nominal"} or
        dtype = {"a": Continuous(), "b": "nominal"}
        or dtype = Continuous() or dtype = "Continuous" or dtype = Continuous()
    """

    # pylint: disable=too-many-instance-attributes
    _ddf: dd.DataFrame
    _values: da.Array
    _nulls: Union[da.Array, np.ndarray]
    _columns: pd.Index
    _eda_dtypes: Dict[str, DType] = {}
    _str_col_cache: Dict[Tuple[str, bool], dd.Series] = {}
    _nulls_cnt: Dict[str, int] = {}
    _head: Optional[pd.DataFrame] = None
    _shape: Optional[Tuple[int, int]] = None

    # pylint: disable = too-many-branches
    def __init__(
        self,
        df: Optional[DataFrame] = None,
        value_length: bool = False,
        repartition: bool = True,
        dtype: Optional[DTypeDef] = None,
    ) -> None:

        _suppress_warnings()

        if df is None:
            return

        if isinstance(df, EDAFrame):
            self._ddf = df._ddf
            self._values = df._values
            self._columns = df._columns
            self._nulls = df._nulls
            self._nulls_cnt = df._nulls_cnt
            self._eda_dtypes = df._eda_dtypes
            self._str_col_cache = df._str_col_cache
            self._head = df._head
            self._shape = df._shape
            return

        if isinstance(df, (dd.Series, pd.Series)):
            df = df.to_frame()

        # if index is object type, convert it to string
        # to make sure the element is comparable. Otherwise it will throw
        # error when dask divide and sort data by index.
        if df.index.dtype == np.object:
            df.index = df.index.astype(str)

        if isinstance(df, dd.DataFrame):
            is_pandas = False
            ddf = df
        elif isinstance(df, pd.DataFrame):
            is_pandas = True
            if repartition:
                df_size = df.memory_usage(deep=True).sum()
                npartitions = ceil(df_size / 128 / 1024 / 1024)
                ddf = dd.from_pandas(df, npartitions=npartitions)
            else:
                ddf = dd.from_pandas(df, chunksize=-1)
        else:
            raise ValueError(f"{type(df)} not supported")

        ddf.columns = _process_column_name(ddf.columns)
        ddf = ddf.persist()
        self._eda_dtypes = _detect_dtypes(ddf, dtype)

        # Transform categorical column to string for non-na values.
        for col in ddf.columns:
            if isinstance(self._eda_dtypes[col], Nominal):
                ddf[col] = ddf[col].apply(_to_str_if_not_na, meta=(col, "object"))

        self._ddf = ddf.persist()
        self._columns = self._ddf.columns
        if value_length or is_pandas:
            self._values = self._ddf.to_dask_array(lengths=True)
        else:
            self._values = self._ddf.to_dask_array()

        # compute meta for null values
        dd_null = self._ddf.isnull()
        self._nulls = dd_null.to_dask_array()
        self._nulls._chunks = self.values.chunks
        pd_null = dd_null.compute()
        nulls_cnt = {}
        for col in self._ddf.columns:
            nulls_cnt[col] = pd_null[col].sum()
        self._nulls_cnt = nulls_cnt

    @property
    def columns(self) -> pd.Index:
        """Return the columns of the DataFrame."""
        return self._columns

    @property
    def dtypes(self) -> pd.Series:
        """Returns the dtypes of the DataFrame."""
        return self._ddf.dtypes

    @property
    def nulls(self) -> da.Array:
        """Return the nullity array of the data."""
        return self._nulls

    @property
    def shape(self) -> Tuple[int, int]:
        """Return the shape of the data"""
        if self._shape is None:
            self._shape = cast(Tuple[int, int], self.values.shape)
        return self._shape

    @property
    def values(self) -> da.Array:
        """Return the array representation of the data."""
        return self._values

    @property
    def frame(self) -> dd.DataFrame:
        """Return the underlying dataframe."""
        return self._ddf

    def head(self, n: int = 5) -> pd.DataFrame:
        """Return the head of the DataFrame, if not exist, read it."""
        if self._head is None:
            self._head = self.frame.head(n=n)
        return self._head

    def get_col_as_str(self, col: str, na_as_str: bool = False) -> dd.Series:
        """
        Return the column as string column.
        If na_as_str is True, then NA vlaues will also be transformed to str,
        otherwise it is kept as NA.
        """
        if col not in self._columns:
            raise RuntimeError(f"column is not exists: {col}")

        if (col, na_as_str) in self._str_col_cache:
            return self._str_col_cache[(col, na_as_str)]

        # The case for directly return
        if (isinstance(self._eda_dtypes[col], Nominal)) and (
            (na_as_str and self.get_missing_cnt(col) == 0) or (not na_as_str)
        ):
            return self._ddf[col]

        if na_as_str:
            self._str_col_cache[(col, na_as_str)] = self._ddf[col].astype(str).persist()
        else:
            self._str_col_cache[(col, na_as_str)] = (
                self._ddf[col].apply(_to_str_if_not_na, meta=(col, "object")).persist()
            )

        return self._str_col_cache[(col, na_as_str)]

    def get_missing_cnt(self, col: str) -> int:
        """
        Get the count of missing values for given column.
        """
        return self._nulls_cnt[col]

    def get_dtype(self, col: str) -> DType:
        """
        Get the infered dtype for the given column.
        """
        return self._eda_dtypes[col]

    def compute(self, type: str = "lengths") -> None:  # pylint: disable=redefined-builtin
        """Compute the lengths or materialize the null values inplace.

        Parameters
        ----------
        type
            Can be lengths or nulls. lengths will compute the array chunk sizes and nulls
            will compute and materialize the null values as well as the lengths of the chunks.

        """

        # pylint: disable = protected-access
        if type == "lengths":
            not_computed = np.isnan(self.shape[0])
            if not_computed:
                self._values = self.frame.to_dask_array(lengths=True)
                self._nulls = self.frame.isnull().to_dask_array()
                self._nulls._chunks = self.values.chunks
        elif type == "nulls":
            x = self.nulls
            # Copied from compute_chunk_sizes
            # pylint: disable=invalid-name
            chunk_shapes = x.map_blocks(
                _get_chunk_shape,
                dtype=int,
                chunks=tuple(len(c) * (1,) for c in x.chunks) + ((x.ndim,),),
                new_axis=x.ndim,
            )

            c = []
            for i in range(x.ndim):
                s = x.ndim * [0] + [i]
                s[i] = slice(None)
                s = tuple(s)

                c.append(tuple(chunk_shapes[s]))

            chunks_, nulls = dask.compute(tuple(c), self.nulls)
            chunks = tuple([tuple([int(chunk) for chunk in chunks]) for chunks in chunks_])
            self._nulls = nulls
            self._values._chunks = chunks
        else:
            raise ValueError(f"{type} not supported.")

    def select_dtypes(self, include: List[Any]) -> "EDAFrame":
        """Return a new DataArray with designated dtype columns."""
        subdf = self._ddf.select_dtypes(include)  # pylint: disable=W0212
        return self[subdf.columns]

    def select_num_columns(self) -> "EDAFrame":
        """Return a new DataArray with numerical dtype columns."""
        df = self.select_dtypes(NUMERICAL_DTYPES)
        return df

    def __getitem__(self, indexer: Union[Sequence[str], str]) -> "EDAFrame":
        """Return a new DataArray select by column names."""
        if isinstance(indexer, str):
            indexer = [indexer]

        subdf = self._ddf[indexer]  # pylint: disable=W0212
        cidx = [self.columns.get_loc(col) for col in subdf.columns]
        df = EDAFrame()
        df._ddf = subdf
        df._columns = subdf.columns
        df._values = self.values[:, cidx]  # pylint: disable=W0212
        df._nulls = self.nulls[:, cidx]  # pylint: disable=W0212
        if self._head is not None:
            df._head = self.head()[subdf.columns]  # pylint: disable=W0212

        eda_dtypes: Dict[str, DType] = {}
        str_col_cache: Dict[Tuple[str, bool], dd.Series] = {}
        nulls_cnt: Dict[str, int] = {}
        for col in df._columns:
            eda_dtypes[col] = self._eda_dtypes[col]
            nulls_cnt[col] = self._nulls_cnt[col]
            for val in [True, False]:
                if (col, val) in self._str_col_cache:
                    str_col_cache[(col, val)] = self._str_col_cache[(col, val)]

        df._eda_dtypes = eda_dtypes
        df._str_col_cache = str_col_cache
        df._nulls_cnt = nulls_cnt

        if df.shape[1] != 0:
            # coerce the array to it's minimal type
            dtype = reduce(np.promote_types, df.dtypes.values)
            if df._values.dtype != dtype:
                df._values = df._values.astype(dtype)

        return df


def _get_chunk_shape(arr: np.ndarray) -> np.ndarray:
    """Given an (x,y,...) N-d array, returns (1,1,...,N) N+1-d array"""
    shape = np.asarray(arr.shape, dtype=int)
    return shape[len(shape) * (None,) + (slice(None),)]


def _process_column_name(df_columns: pd.Index) -> List[str]:
    """
    1.  Transform column name to string,
    2.  Resolve duplicate names in columns.
        Duplicate names will be renamed as col_{id}.
    """
    columns = list(map(str, df_columns))
    column_count = Counter(columns)
    current_id: Dict[Any, int] = dict()
    for i, col in enumerate(columns):
        if column_count[col] > 1:
            current_id[col] = current_id.get(col, 0) + 1
            new_col_name = f"{col}_{current_id[col]}"
        else:
            new_col_name = f"{col}"
        columns[i] = new_col_name
    return columns


def _to_str_if_not_na(obj: Any) -> Any:
    """
    This function transforms an obj to str if it is not NA.
    The check for NA is similar to pd.isna, but will treat a list obj as
    a scalar and return a single boolean, rather than a list of booleans.
    Otherwise when a cell is tuple or list it will throw an error.
    """
    return obj if libmissing.checknull(obj) else str(obj)


def _detect_dtypes(df: dd.DataFrame, known_dtype: Optional[DTypeDef] = None) -> Dict[str, DType]:
    """
    Return a dict that maps column name to its dtype for each column in given df.
    """
    head = df.head(n=100)
    res = {}
    for col in df.columns:
        dtype = detect_dtype(df[col], head[col], known_dtype)
        res[col] = dtype
    return res


def _suppress_warnings() -> None:
    """
    suppress warnings
    """
    warnings.filterwarnings(
        "ignore",
        "Insufficient elements for `head`.",
        category=UserWarning,
    )
