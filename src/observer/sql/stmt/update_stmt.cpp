/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MARCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#include "sql/stmt/update_stmt.h"

#include "common/log/log.h"
#include "sql/stmt/filter_stmt.h"
#include "storage/db/db.h"
#include "storage/table/table.h"

UpdateStmt::UpdateStmt(Table *table, const FieldMeta *field_meta, const Value &value, FilterStmt *filter_stmt)
    : table_(table), field_meta_(field_meta), value_(value), filter_stmt_(filter_stmt)
{}

UpdateStmt::~UpdateStmt()
{
  if (filter_stmt_ != nullptr) {
    delete filter_stmt_;
    filter_stmt_ = nullptr;
  }
}

RC UpdateStmt::create(Db *db, const UpdateSqlNode &update, Stmt *&stmt)
{
  stmt = nullptr;
  if (db == nullptr || update.relation_name.empty() || update.attribute_name.empty()) {
    return RC::INVALID_ARGUMENT;
  }

  Table *table = db->find_table(update.relation_name.c_str());
  if (table == nullptr) {
    LOG_WARN("no such table. db=%s, table=%s", db->name(), update.relation_name.c_str());
    return RC::SCHEMA_TABLE_NOT_EXIST;
  }

  const FieldMeta *field_meta = table->table_meta().field(update.attribute_name.c_str());
  if (field_meta == nullptr) {
    LOG_WARN("no such field. table=%s, field=%s", update.relation_name.c_str(), update.attribute_name.c_str());
    return RC::SCHEMA_FIELD_NOT_EXIST;
  }

  Value value = update.value;
  if (value.attr_type() != field_meta->type()) {
    RC rc = Value::cast_to(update.value, field_meta->type(), value);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to cast update value. field=%s, rc=%s", update.attribute_name.c_str(), strrc(rc));
      return rc;
    }
  }

  unordered_map<string, Table *> table_map;
  table_map.insert(pair<string, Table *>(update.relation_name, table));

  FilterStmt *filter_stmt = nullptr;
  RC          rc          = FilterStmt::create(
      db, table, &table_map, update.conditions.data(), static_cast<int>(update.conditions.size()), filter_stmt);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to create filter statement. rc=%s", strrc(rc));
    return rc;
  }

  stmt = new UpdateStmt(table, field_meta, value, filter_stmt);
  return RC::SUCCESS;
}
