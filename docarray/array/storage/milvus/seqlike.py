from typing import Iterable, Iterator, Union, TYPE_CHECKING
from docarray.array.storage.base.seqlike import BaseSequenceLikeMixin

if TYPE_CHECKING:
    from docarray import Document


class SequenceLikeMixin(BaseSequenceLikeMixin):
    def __eq__(self, other):
        """Compare this object to the other, returns True if and only if other
        as the same type as self and other have the same Milvus Collections for data and offset2id

        :param other: the other object to check for equality
        :return: `True` if other is equal to self
        """
        return (
            type(self) is type(other)
            and self._collection.name == other._collection.name
            and self._offset2id_collection.name == other._offset2id_collection.name
            and self._config == other._config
        )

    def __contains__(self, x: Union[str, 'Document']):
        if isinstance(x, Document):
            x = x.id
        try:
            self._get_doc_by_id(x)
            return True
        except:  # TODO(johannes) make exception more specific
            return False

    def __repr__(self):
        return f'<DocumentArray[Milvus] (length={len(self)}) at {id(self)}>'

    def __add__(self, other: Union['Document', Iterable['Document']]):
        if isinstance(other, Document):
            self.append(other)
        else:
            self.extend(other)
        return self

    #
    # def __len__(self):
    #     return self._collection.num_entities  # This doesn't work for some reason. Currently in contact with Milvus team to resolve.
