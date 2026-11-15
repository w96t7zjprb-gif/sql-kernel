#include "sql/stmt/drop_table_stmt.h"

#include "common/log/log.h"
#include "storage/db/db.h"

RC DropTableStmt::create(Db *db, const DropTableSqlNode &drop_table, Stmt *&stmt)
{
  if (nullptr == db) {
    return RC::INVALID_ARGUMENT;
  }

  Table *table = db->find_table(drop_table.relation_name.c_str());
  if (nullptr == table) {
    LOG_WARN("table not found. table=%s", drop_table.relation_name.c_str());
    return RC::SCHEMA_TABLE_NOT_EXIST;
  }

  stmt = new DropTableStmt(drop_table.relation_name);
  return RC::SUCCESS;
}
