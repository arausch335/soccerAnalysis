# Checkpoints embed provider JSON in CSV cells, often larger than 128 KiB.
# INT32_MAX fits Windows C long as well as Unix; setting the cap allocates no buffer.
import csv
csv.field_size_limit(max(csv.field_size_limit(),2**31-1))

from .pandasStore import PandasStore

__all__ = ['PandasStore']
