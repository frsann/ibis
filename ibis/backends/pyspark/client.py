from __future__ import annotations

from typing import Any, Iterable, Mapping

import pandas as pd
import pyspark as ps

import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.schema as sch
import ibis.expr.types as ir
from ibis.backends.base.sql.ddl import fully_qualified_re
from ibis.backends.pyspark import ddl


@sch.infer.register(ps.sql.dataframe.DataFrame)
def spark_dataframe_schema(df):
    """Infer the schema of a Spark SQL `DataFrame` object."""
    # df.schema is a pt.StructType
    schema_struct = dt.dtype(df.schema)

    return sch.schema(schema_struct.names, schema_struct.types)


class PySparkTable(ir.Table):
    @property
    def _qualified_name(self):
        return self.op().args[0]

    def _match_name(self):
        m = fully_qualified_re.match(self._qualified_name)
        if not m:
            return None, self._qualified_name
        db, quoted, unquoted = m.groups()
        return db, quoted or unquoted

    @property
    def _database(self):
        return self._match_name()[0]

    @property
    def _unqualified_name(self):
        return self._match_name()[1]

    @property
    def name(self):
        return self.op().name

    @property
    def _client(self):
        return self.op().source

    def compute_stats(self, noscan: bool = False):
        """Invoke the Spark ANALYZE TABLE <tbl> COMPUTE STATISTICS command.

        See Also
        --------
        SparkClient.compute_stats
        """
        return self._client.compute_stats(self._qualified_name, noscan=noscan)

    def drop(self):
        """Drop the table from the database."""
        self._client.drop_table_or_view(self._qualified_name)

    def truncate(self):
        self._client.truncate_table(self._qualified_name)

    @staticmethod
    def _validate_compatible(from_schema, to_schema):
        if set(from_schema.names) != set(to_schema.names):
            raise com.IbisInputError('Schemas have different names')

        for name in from_schema:
            lt = from_schema[name]
            rt = to_schema[name]
            if not dt.castable(lt, rt):
                raise com.IbisInputError(f'Cannot safely cast {lt!r} to {rt!r}')

    def insert(
        self,
        obj: ir.Table | pd.DataFrame | None = None,
        overwrite: bool = False,
        values: Iterable[Any] | None = None,
        validate: bool = True,
    ):
        """Insert data into the table.

        Parameters
        ----------
        obj
            Table expression or pandas DataFrame
        overwrite
            If True, will replace existing contents of table
        values
            Values to insert. Not implemented currently.
        validate
            If True, do more rigorous validation that schema of table being
            inserted is compatible with the existing table

        Examples
        --------
        >>> t.insert(table_expr)  # doctest: +SKIP

        # Completely overwrite contents
        >>> t.insert(table_expr, overwrite=True)  # doctest: +SKIP
        """
        if isinstance(obj, pd.DataFrame):
            spark_df = self._session.createDataFrame(obj)
            spark_df.insertInto(self.name, overwrite=overwrite)
            return

        expr = obj

        if values is not None:
            raise NotImplementedError

        if validate:
            existing_schema = self.schema()
            insert_schema = expr.schema()
            if not insert_schema.equals(existing_schema):
                self._validate_compatible(insert_schema, existing_schema)

        ast = self._client.compiler.to_ast(expr)
        select = ast.queries[0]
        statement = ddl.InsertSelect(self._qualified_name, select, overwrite=overwrite)
        return self._client.raw_sql(statement.compile())

    def rename(self, new_name: str) -> PySparkTable:
        """Rename the table inside Spark.

        References to the old table are no longer valid. Spark does not support
        moving tables across databases using `rename`.

        Parameters
        ----------
        new_name
            New table name

        Returns
        -------
        PySparkTable
            Renamed spark table
        """
        new_qualified_name = self._client._fully_qualified_name(
            new_name, self._database
        )

        statement = ddl.RenameTable(self._qualified_name, new_name)
        self._client.raw_sql(statement.compile())

        op = self.op().change_name(new_qualified_name)
        return type(self)(op)

    def alter(self, tbl_properties: Mapping[str, str] | None = None) -> Any:
        """Change settings and parameters of the table.

        Parameters
        ----------
        tbl_properties
            Spark table properties
        """

        stmt = ddl.AlterTable(self._qualified_name, tbl_properties=tbl_properties)
        return self._client.raw_sql(stmt.compile())
