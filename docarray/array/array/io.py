import base64
import csv
import io
import json
import os
import pathlib
import pickle
from abc import abstractmethod
from contextlib import nullcontext
from itertools import compress
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    ContextManager,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import pandas as pd

from docarray.base_document import AnyDocument, BaseDocument
from docarray.helper import (
    _access_path_to_dict,
    _dict_to_access_paths,
    _update_nested_dicts,
    is_access_path_valid,
)
from docarray.utils.compress import _decompress_bytes, _get_compress_ctx

if TYPE_CHECKING:

    from docarray import DocumentArray
    from docarray.proto import DocumentArrayProto

T = TypeVar('T', bound='IOMixinArray')

ARRAY_PROTOCOLS = {'protobuf-array', 'pickle-array'}
SINGLE_PROTOCOLS = {'pickle', 'protobuf'}
ALLOWED_PROTOCOLS = ARRAY_PROTOCOLS.union(SINGLE_PROTOCOLS)
ALLOWED_COMPRESSIONS = {'lz4', 'bz2', 'lzma', 'zlib', 'gzip'}


def _protocol_and_compress_from_file_path(
    file_path: Union[pathlib.Path, str],
    default_protocol: Optional[str] = None,
    default_compress: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract protocol and compression algorithm from a string, use defaults if not found.
    :param file_path: path of a file.
    :param default_protocol: default serialization protocol used in case not found.
    :param default_compress: default compression method used in case not found.
    Examples:
    >>> _protocol_and_compress_from_file_path('./docarray_fashion_mnist.protobuf.gzip')
    ('protobuf', 'gzip')
    >>> _protocol_and_compress_from_file_path('/Documents/docarray_fashion_mnist.protobuf')
    ('protobuf', None)
    >>> _protocol_and_compress_from_file_path('/Documents/docarray_fashion_mnist.gzip')
    (None, gzip)
    """

    protocol = default_protocol
    compress = default_compress

    file_extensions = [e.replace('.', '') for e in pathlib.Path(file_path).suffixes]
    for extension in file_extensions:
        if extension in ALLOWED_PROTOCOLS:
            protocol = extension
        elif extension in ALLOWED_COMPRESSIONS:
            compress = extension

    return protocol, compress


class _LazyRequestReader:
    def __init__(self, r):
        self._data = r.iter_content(chunk_size=1024 * 1024)
        self.content = b''

    def __getitem__(self, item: slice):
        while len(self.content) < item.stop:
            try:
                self.content += next(self._data)
            except StopIteration:
                return self.content[item.start : -1 : item.step]
        return self.content[item]


class IOMixinArray(Iterable[BaseDocument]):

    document_type: Type[BaseDocument]

    @abstractmethod
    def __len__(self):
        ...

    @abstractmethod
    def __init__(
        self,
        docs: Optional[Iterable[BaseDocument]] = None,
    ):
        ...

    @classmethod
    def from_protobuf(cls: Type[T], pb_msg: 'DocumentArrayProto') -> T:
        """create a Document from a protobuf message
        :param pb_msg: The protobuf message from where to construct the DocumentArray
        """
        return cls(
            cls.document_type.from_protobuf(doc_proto) for doc_proto in pb_msg.docs
        )

    def to_protobuf(self) -> 'DocumentArrayProto':
        """Convert DocumentArray into a Protobuf message"""
        from docarray.proto import DocumentArrayProto

        da_proto = DocumentArrayProto()
        for doc in self:
            da_proto.docs.append(doc.to_protobuf())

        return da_proto

    @classmethod
    def from_bytes(
        cls: Type[T],
        data: bytes,
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> T:
        """Deserialize bytes into a DocumentArray.

        :param data: Bytes from which to deserialize
        :param protocol: protocol that was used to serialize
        :param compress: compress algorithm that was used to serialize
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: the deserialized DocumentArray
        """
        return cls._load_binary_all(
            file_ctx=nullcontext(data),
            protocol=protocol,
            compress=compress,
            show_progress=show_progress,
        )

    def _write_bytes(
        self,
        bf: BinaryIO,
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> None:
        if protocol in ARRAY_PROTOCOLS:
            compress_ctx = _get_compress_ctx(compress)
        else:
            # delegate the compression to per-doc compression
            compress_ctx = None

        fc: ContextManager
        if compress_ctx is None:
            # if compress do not support streaming then postpone the compress
            # into the for-loop
            f, fc = bf, nullcontext()
        else:
            f = compress_ctx(bf)
            fc = f
            compress = None

        with fc:
            if protocol == 'protobuf-array':
                f.write(self.to_protobuf().SerializePartialToString())
            elif protocol == 'pickle-array':
                f.write(pickle.dumps(self))
            elif protocol in SINGLE_PROTOCOLS:
                from rich import filesize

                from docarray.utils.progress_bar import _get_progressbar

                pbar, t = _get_progressbar(
                    'Serializing', disable=not show_progress, total=len(self)
                )

                f.write(self._stream_header)

                with pbar:
                    _total_size = 0
                    pbar.start_task(t)
                    for doc in self:
                        doc_bytes = doc.to_bytes(protocol=protocol, compress=compress)
                        len_doc_as_bytes = len(doc_bytes).to_bytes(
                            4, 'big', signed=False
                        )
                        all_bytes = len_doc_as_bytes + doc_bytes
                        f.write(all_bytes)
                        _total_size += len(all_bytes)
                        pbar.update(
                            t,
                            advance=1,
                            total_size=str(filesize.decimal(_total_size)),
                        )
            else:
                raise ValueError(
                    f'protocol={protocol} is not supported. Can be only {ALLOWED_PROTOCOLS}.'
                )

    def to_bytes(
        self,
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        file_ctx: Optional[BinaryIO] = None,
        show_progress: bool = False,
    ) -> Optional[bytes]:
        """Serialize itself into bytes.

        For more Pythonic code, please use ``bytes(...)``.

        :param protocol: protocol to use. It can be 'pickle-array', 'protobuf-array', 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param file_ctx: File or filename or serialized bytes where the data is stored.
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: the binary serialization in bytes or None if file_ctx is passed where to store
        """

        with (file_ctx or io.BytesIO()) as bf:
            self._write_bytes(
                bf=bf,
                protocol=protocol,
                compress=compress,
                show_progress=show_progress,
            )
            if isinstance(bf, io.BytesIO):
                return bf.getvalue()

        return None

    @classmethod
    def from_base64(
        cls: Type[T],
        data: str,
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> T:
        """Deserialize base64 strings into a DocumentArray.

        :param data: Base64 string to deserialize
        :param protocol: protocol that was used to serialize
        :param compress: compress algorithm that was used to serialize
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: the deserialized DocumentArray
        """
        return cls._load_binary_all(
            file_ctx=nullcontext(base64.b64decode(data)),
            protocol=protocol,
            compress=compress,
            show_progress=show_progress,
        )

    def to_base64(
        self,
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> str:
        """Serialize itself into base64 encoded string.

        :param protocol: protocol to use. It can be 'pickle-array', 'protobuf-array', 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: the binary serialization in bytes or None if file_ctx is passed where to store
        """
        with io.BytesIO() as bf:
            self._write_bytes(
                bf=bf,
                compress=compress,
                protocol=protocol,
                show_progress=show_progress,
            )
            return base64.b64encode(bf.getvalue()).decode('utf-8')

    @classmethod
    def from_json(
        cls: Type[T],
        file: Union[str, bytes, bytearray],
    ) -> T:
        """Deserialize JSON strings or bytes into a DocumentArray.

        :param file: JSON object from where to deserialize a DocumentArray
        :return: the deserialized DocumentArray
        """
        json_docs = json.loads(file)
        return cls([cls.document_type.parse_raw(v) for v in json_docs])

    def to_json(self) -> str:
        """Convert the object into a JSON string. Can be loaded via :meth:`.from_json`.
        :return: JSON serialization of DocumentArray
        """
        return json.dumps([doc.json() for doc in self])

    @classmethod
    def _check_for_valid_document_type(cls) -> None:
        if cls.document_type == AnyDocument:
            raise TypeError(
                'There is no document schema defined. '
                'Please specify the DocumentArray\'s Document type using `DocumentArray[MyDoc]`.'
            )

    @classmethod
    def _check_for_valid_access_paths(cls, field_names: Optional[List[str]]) -> None:
        if field_names is None or len(field_names) == 0:
            raise TypeError("No field names are given.")

        valid = [
            is_access_path_valid(cls.document_type, field) for field in field_names
        ]
        if not all(valid):
            raise ValueError(
                f'Column names do not match the schema of the DocumentArray\'s document type '
                f'({cls.document_type.__name__}): {list(compress(field_names, [not v for v in valid]))}'
            )

    @staticmethod
    def access_path_dict_to_nested_dict(
        access_path2val: Dict[str, Any]
    ) -> Dict[Any, Any]:
        """
        Convert a dict, where the keys are access paths ("__"-separated) to a nested dictionary.

        EXAMPLE USAGE

        .. code-block:: python

            access_path2val = {'image__url': 'some.png'}
            assert access_path_dict_to_nested_dict(access_path2val) == {
                'image': {'url': 'some.png'}
            }

        :param access_path2val: dict with access_paths as keys
        :return: nested dict where the access path keys are split into separate field names and nested keys
        """
        nested_dict: Dict[Any, Any] = {}
        for access_path, value in access_path2val.items():
            field2val = _access_path_to_dict(
                access_path=access_path,
                value=value if value not in ['', 'None'] else None,
            )
            _update_nested_dicts(to_update=nested_dict, update_with=field2val)
        return nested_dict

    @classmethod
    def from_csv(
        cls,
        file_path: str,
        encoding: str = 'utf-8',
        dialect: Union[str, csv.Dialect] = 'excel',
    ) -> 'DocumentArray':
        """
        Load a DocumentArray from a csv file following the schema defined in the
        :attr:`~docarray.DocumentArray.document_type` attribute.
        Every row of the csv file will be mapped to one document in the array.
        The column names (defined in the first row) have to match the field names
        of the Document type.
        For nested fields use "__"-separated access paths, such as 'image__url'.

        List-like fields (including field of type DocumentArray) are not supported.

        :param file_path: path to csv file to load DocumentArray from.
        :param encoding: encoding used to read the csv file. Defaults to 'utf-8'.
        :param dialect: defines separator and how to handle whitespaces etc.
            Can be a csv.Dialect instance or one string of:
            'excel' (for comma seperated values),
            'excel-tab' (for tab separated values),
            'unix' (for csv file generated on UNIX systems).
        :return: DocumentArray
        """
        from docarray import DocumentArray

        cls._check_for_valid_document_type()

        doc_type = cls.document_type
        da = DocumentArray.__class_getitem__(doc_type)()

        with open(file_path, 'r', encoding=encoding) as fp:
            rows = csv.DictReader(fp, dialect=dialect)
            field_names: List[str] = (
                [] if rows.fieldnames is None else [str(f) for f in rows.fieldnames]
            )

            cls._check_for_valid_access_paths(field_names=field_names)

            for access_path2val in rows:
                doc_dict: Dict[Any, Any] = IOMixinArray.access_path_dict_to_nested_dict(
                    access_path2val
                )
                da.append(doc_type.parse_obj(doc_dict))

        return da

    def to_csv(
        self, file_path: str, dialect: Union[str, csv.Dialect] = 'excel'
    ) -> None:
        """
        Save a DocumentArray to a csv file.
        The field names will be stored in the first row. Each row corresponds to the
        information of one Document.
        Columns for nested fields will be named after the "__"-seperated access paths,
        such as `"image__url"` for `image.url`.

        :param file_path: path to a csv file.
        :param dialect: defines separator and how to handle whitespaces etc.
            Can be a csv.Dialect instance or one string of:
            'excel' (for comma seperated values),
            'excel-tab' (for tab separated values),
            'unix' (for csv file generated on UNIX systems).
        """
        fields = self.document_type._get_access_paths()

        with open(file_path, 'w') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fields, dialect=dialect)
            writer.writeheader()

            for doc in self:
                doc_dict = _dict_to_access_paths(doc.dict())
                writer.writerow(doc_dict)

    @classmethod
    def from_pandas(cls, df: pd.DataFrame) -> 'DocumentArray':
        """
        Load a DocumentArray from a `pandas.DataFrame` following the schema
        defined in the :attr:`~docarray.DocumentArray.document_type` attribute.
        Every row of the dataframe will be mapped to one document in the array.
        The column names of the dataframe have to match the field namesof the
        Document type.
        For nested fields use "__"-separated access paths as column names,
        such as 'image__url'.

        List-like fields (including field of type DocumentArray) are not supported.

        :param df: pandas.DataFrame to extract Document's information from
        :return: DocumentArray
        """
        from docarray import DocumentArray

        cls._check_for_valid_document_type()

        doc_type = cls.document_type
        da = DocumentArray.__class_getitem__(doc_type)()
        field_names = df.columns.tolist()

        cls._check_for_valid_access_paths(field_names=field_names)

        for row in df.itertuples():
            access_path2val = row._asdict()
            access_path2val.pop('Index', None)
            doc_dict = IOMixinArray.access_path_dict_to_nested_dict(access_path2val)
            da.append(doc_type.parse_obj(doc_dict))

        return da

    def to_pandas(self) -> pd.DataFrame:
        """
        Save a DocumentArray to a `pandas.DataFrame`.
        The field names will be stored as column names. Each row of the dataframe corresponds
        to the information of one Document.
        Columns for nested fields will be named after the "__"-seperated access paths,
        such as `"image__url"` for `image.url`.

        :return: pandas.DataFrame
        """
        fields = self.document_type._get_access_paths()
        df = pd.DataFrame(columns=fields)

        for doc in self:
            doc_dict = _dict_to_access_paths(doc.dict())
            df = df.append(doc_dict, ignore_index=True)

        return df

    # Methods to load from/to files in different formats
    @property
    def _stream_header(self) -> bytes:
        # Binary format for streaming case

        # V1 DocArray streaming serialization format
        # | 1 byte | 8 bytes | 4 bytes | variable | 4 bytes | variable ...

        # 1 byte (uint8)
        version_byte = b'\x01'
        # 8 bytes (uint64)
        num_docs_as_bytes = len(self).to_bytes(8, 'big', signed=False)
        return version_byte + num_docs_as_bytes

    @classmethod
    def _load_binary_all(
        cls: Type[T],
        file_ctx: Union[ContextManager[io.BufferedReader], ContextManager[bytes]],
        protocol: Optional[str],
        compress: Optional[str],
        show_progress: bool,
    ):
        """Read a `DocumentArray` object from a binary file
        :param protocol: protocol to use. It can be 'pickle-array', 'protobuf-array', 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: a `DocumentArray`
        """
        with file_ctx as fp:
            if isinstance(fp, bytes):
                d = fp
            else:
                d = fp.read()

        if protocol is not None and protocol in ('pickle-array', 'protobuf-array'):
            if _get_compress_ctx(algorithm=compress) is not None:
                d = _decompress_bytes(d, algorithm=compress)
                compress = None

        if protocol is not None and protocol == 'protobuf-array':
            from docarray.proto import DocumentArrayProto

            dap = DocumentArrayProto()
            dap.ParseFromString(d)

            return cls.from_protobuf(dap)
        elif protocol is not None and protocol == 'pickle-array':
            return pickle.loads(d)

        # Binary format for streaming case
        else:
            from rich import filesize

            from docarray.utils.progress_bar import _get_progressbar

            # 1 byte (uint8)
            # 8 bytes (uint64)
            num_docs = int.from_bytes(d[1:9], 'big', signed=False)

            pbar, t = _get_progressbar(
                'Deserializing', disable=not show_progress, total=num_docs
            )

            # this 9 is version + num_docs bytes used
            start_pos = 9
            docs = []
            with pbar:
                _total_size = 0
                pbar.start_task(t)

                for _ in range(num_docs):
                    # 4 bytes (uint32)
                    len_current_doc_in_bytes = int.from_bytes(
                        d[start_pos : start_pos + 4], 'big', signed=False
                    )
                    start_doc_pos = start_pos + 4
                    end_doc_pos = start_doc_pos + len_current_doc_in_bytes
                    start_pos = end_doc_pos

                    # variable length bytes doc
                    load_protocol: str = protocol or 'protobuf'
                    doc = cls.document_type.from_bytes(
                        d[start_doc_pos:end_doc_pos],
                        protocol=load_protocol,
                        compress=compress,
                    )
                    docs.append(doc)
                    _total_size += len_current_doc_in_bytes
                    pbar.update(
                        t, advance=1, total_size=str(filesize.decimal(_total_size))
                    )
            return cls(docs)

    @classmethod
    def _load_binary_stream(
        cls: Type[T],
        file_ctx: ContextManager[io.BufferedReader],
        protocol: Optional[str] = None,
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> Generator['BaseDocument', None, None]:
        """Yield `Document` objects from a binary file

        :param protocol: protocol to use. It can be 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :return: a generator of `Document` objects
        """

        from rich import filesize

        from docarray import BaseDocument
        from docarray.utils.progress_bar import _get_progressbar

        with file_ctx as f:
            version_numdocs_lendoc0 = f.read(9)
            # 1 byte (uint8)
            # 8 bytes (uint64)
            num_docs = int.from_bytes(version_numdocs_lendoc0[1:9], 'big', signed=False)

            pbar, t = _get_progressbar(
                'Deserializing', disable=not show_progress, total=num_docs
            )

            with pbar:
                _total_size = 0
                pbar.start_task(t)
                for _ in range(num_docs):
                    # 4 bytes (uint32)
                    len_current_doc_in_bytes = int.from_bytes(
                        f.read(4), 'big', signed=False
                    )
                    _total_size += len_current_doc_in_bytes
                    load_protocol: str = protocol or 'protobuf'
                    yield BaseDocument.from_bytes(
                        f.read(len_current_doc_in_bytes),
                        protocol=load_protocol,
                        compress=compress,
                    )
                    pbar.update(
                        t, advance=1, total_size=str(filesize.decimal(_total_size))
                    )

    @classmethod
    def load_binary(
        cls: Type[T],
        file: Union[str, bytes, pathlib.Path, io.BufferedReader, _LazyRequestReader],
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
        streaming: bool = False,
    ) -> Union[T, Generator['BaseDocument', None, None]]:
        """Load array elements from a compressed binary file.

        :param file: File or filename or serialized bytes where the data is stored.
        :param protocol: protocol to use. It can be 'pickle-array', 'protobuf-array', 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`
        :param streaming: if `True` returns a generator over `Document` objects.
        In case protocol is pickle the `Documents` are streamed from disk to save memory usage
        :return: a DocumentArray object

        .. note::
            If `file` is `str` it can specify `protocol` and `compress` as file extensions.
            This functionality assumes `file=file_name.$protocol.$compress` where `$protocol` and `$compress` refer to a
            string interpolation of the respective `protocol` and `compress` methods.
            For example if `file=my_docarray.protobuf.lz4` then the binary data will be loaded assuming `protocol=protobuf`
            and `compress=lz4`.
        """
        load_protocol: Optional[str] = protocol
        load_compress: Optional[str] = compress
        file_ctx: Union[nullcontext, io.BufferedReader]
        if isinstance(file, (io.BufferedReader, _LazyRequestReader, bytes)):
            file_ctx = nullcontext(file)
        # by checking path existence we allow file to be of type Path, LocalPath, PurePath and str
        elif isinstance(file, (str, pathlib.Path)) and os.path.exists(file):
            load_protocol, load_compress = _protocol_and_compress_from_file_path(
                file, protocol, compress
            )
            file_ctx = open(file, 'rb')
        else:
            raise FileNotFoundError(f'cannot find file {file}')
        if streaming:
            return cls._load_binary_stream(
                file_ctx,
                protocol=load_protocol,
                compress=load_compress,
                show_progress=show_progress,
            )
        else:
            return cls._load_binary_all(
                file_ctx, load_protocol, load_compress, show_progress
            )

    def save_binary(
        self,
        file: Union[str, pathlib.Path],
        protocol: str = 'protobuf-array',
        compress: Optional[str] = None,
        show_progress: bool = False,
    ) -> None:
        """Save DocumentArray into a binary file.

        It will use the protocol to pick how to save the DocumentArray.
        If used 'picke-array` and `protobuf-array` the DocumentArray will be stored
        and compressed at complete level using `pickle` or `protobuf`.
        When using `protobuf` or `pickle` as protocol each Document in DocumentArray
        will be stored individually and this would make it available for streaming.

        :param file: File or filename to which the data is saved.
        :param protocol: protocol to use. It can be 'pickle-array', 'protobuf-array', 'pickle' or 'protobuf'
        :param compress: compress algorithm to use
        :param show_progress: show progress bar, only works when protocol is `pickle` or `protobuf`

         .. note::
            If `file` is `str` it can specify `protocol` and `compress` as file extensions.
            This functionality assumes `file=file_name.$protocol.$compress` where `$protocol` and `$compress` refer to a
            string interpolation of the respective `protocol` and `compress` methods.
            For example if `file=my_docarray.protobuf.lz4` then the binary data will be created using `protocol=protobuf`
            and `compress=lz4`.
        """
        if isinstance(file, io.BufferedWriter):
            file_ctx = nullcontext(file)
        else:
            _protocol, _compress = _protocol_and_compress_from_file_path(file)

            if _protocol is not None:
                protocol = _protocol
            if _compress is not None:
                compress = _compress

            file_ctx = open(file, 'wb')

        self.to_bytes(
            protocol=protocol,
            compress=compress,
            file_ctx=file_ctx,
            show_progress=show_progress,
        )
