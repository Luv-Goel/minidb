# SQL Reference

minidb supports a subset of standard SQL.

## DDL
- `CREATE TABLE [IF NOT EXISTS] name (col1 type, col2 type)`
- `DROP TABLE [IF EXISTS] name`

## DML
- `INSERT INTO table (col1, col2) VALUES (val1, val2), ...`
- `UPDATE table SET col1 = val1 WHERE ...`
- `DELETE FROM table WHERE ...`

## Queries
- `SELECT ... FROM ... JOIN ... ON ... WHERE ... GROUP BY ... HAVING ... ORDER BY ... LIMIT ... OFFSET ...`

### Scalar Functions
- `upper(str)`
- `lower(str)`
- `length(str)`
- `abs(num)`
- `round(num, digits)`
- `trim(str)`
- `ltrim(str)`
- `rtrim(str)`
- `concat(str1, str2, ...)`
- `coalesce(val1, val2, ...)`
- `random()`

## Utilities
- `EXPLAIN <query>`: Prints the AST plan.
