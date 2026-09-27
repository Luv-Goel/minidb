# Architecture

minidb is designed to be a readable but authentic implementation of a relational database engine.

```mermaid
flowchart TD
    SQL(SQL Query) --> Lexer
    Lexer --> Parser
    Parser --> AST
    AST --> PlannerExecutor
    
    PlannerExecutor --> BTree(B+ Tree)
    PlannerExecutor --> Tx(Transactions / Undo)
    
    BTree --> Pager(Pager / Buffer Pool)
    Tx --> WAL(WAL / Redo)
    
    Pager --> Disk[(Data File)]
    WAL --> Disk
```

### Components
1. **Parser & Lexer**: Converts raw SQL string to an AST.
2. **Engine**: Traverses the AST. Evaluates expressions and handles push-down filtering.
3. **B+ Tree**: The core index structure.
4. **Pager**: Manages 4KiB pages in a buffer pool.
5. **WAL**: Write-ahead-log ensuring durability and crash recovery.
