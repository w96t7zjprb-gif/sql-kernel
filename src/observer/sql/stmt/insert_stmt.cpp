/* Copyright (c) 2021OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MARCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

//
// Created by Wangyunlai on 2022/5/22.
//

#include "sql/stmt/insert_stmt.h"
#include "common/log/log.h"
#include "storage/db/db.h"
#include "storage/table/table.h"

InsertStmt::InsertStmt(Table *table, vector<vector<Value>> &&value_groups)
    : table_(table), value_groups_(std::move(value_groups))
{}

RC InsertStmt::create(Db *db, const InsertSqlNode &inserts, Stmt *&stmt)
{
  const char *table_name = inserts.relation_name.c_str();
  if (nullptr == db || nullptr == table_name || inserts.value_groups.empty()) {
    LOG_WARN("invalid argument. db=%p, table_name=%p, value_group_num=%d",
        db, table_name, static_cast<int>(inserts.value_groups.size()));
    return RC::INVALID_ARGUMENT;
  }

  // check whether the table exists
  Table *table = db->find_table(table_name);
  if (nullptr == table) {
    LOG_WARN("no such table. db=%s, table_name=%s", db->name(), table_name);
    return RC::SCHEMA_TABLE_NOT_EXIST;
  }

  const TableMeta &table_meta = table->table_meta();
  const int        field_num  = table_meta.field_num() - table_meta.sys_field_num();
  for (const vector<Value> &values : inserts.value_groups) {
    if (field_num != static_cast<int>(values.size())) {
      LOG_WARN("schema mismatch. value num=%d, field num in schema=%d",
          static_cast<int>(values.size()), field_num);
      return RC::SCHEMA_FIELD_MISSING;
    }
  }

  vector<vector<Value>> value_groups = inserts.value_groups;
  stmt = new InsertStmt(table, std::move(value_groups));
  return RC::SUCCESS;
}
