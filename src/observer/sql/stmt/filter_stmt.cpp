/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

//
// Created by Wangyunlai on 2022/5/22.
//

#include "sql/stmt/filter_stmt.h"
#include "sql/parser/expression_binder.h"
#include "sql/expr/expression.h"
#include "common/lang/string.h"
#include "common/log/log.h"
#include "common/sys/rc.h"
#include "storage/db/db.h"
#include "storage/table/table.h"

FilterStmt::~FilterStmt()
{
  for (FilterUnit *unit : filter_units_) {
    delete unit;
  }
  filter_units_.clear();
}

RC FilterStmt::create(Db *db, Table *default_table, unordered_map<string, Table *> *tables,
    const ConditionSqlNode *conditions, int condition_num, FilterStmt *&stmt)
{
  RC rc = RC::SUCCESS;
  stmt  = nullptr;

  FilterStmt *tmp_stmt = new FilterStmt();
  for (int i = 0; i < condition_num; i++) {
    FilterUnit *filter_unit = nullptr;

    rc = create_filter_unit(db, default_table, tables, conditions[i], filter_unit);
    if (rc != RC::SUCCESS) {
      delete tmp_stmt;
      LOG_WARN("failed to create filter unit. condition index=%d", i);
      return rc;
    }
    tmp_stmt->filter_units_.push_back(filter_unit);
  }

  stmt = tmp_stmt;
  return rc;
}

RC get_table_and_field(Db *db, Table *default_table, unordered_map<string, Table *> *tables,
    const RelAttrSqlNode &attr, Table *&table, const FieldMeta *&field)
{
  if (common::is_blank(attr.relation_name.c_str())) {
    table = default_table;
  } else if (nullptr != tables) {
    auto iter = tables->find(attr.relation_name);
    if (iter != tables->end()) {
      table = iter->second;
    }
  } else {
    table = db->find_table(attr.relation_name.c_str());
  }
  if (nullptr == table) {
    LOG_WARN("No such table: attr.relation_name: %s", attr.relation_name.c_str());
    return RC::SCHEMA_TABLE_NOT_EXIST;
  }

  field = table->table_meta().field(attr.attribute_name.c_str());
  if (nullptr == field) {
    LOG_WARN("no such field in table: table %s, field %s", table->name(), attr.attribute_name.c_str());
    table = nullptr;
    return RC::SCHEMA_FIELD_NOT_EXIST;
  }

  return RC::SUCCESS;
}

RC FilterStmt::create_filter_unit(Db *db, Table *default_table, unordered_map<string, Table *> *tables,
    const ConditionSqlNode &condition, FilterUnit *&filter_unit)
{
  BinderContext context;
  context.set_db(db);
  if (tables != nullptr) {
    for (const auto &item : *tables) {
      context.add_table(item.second);
    }
  } else if (default_table != nullptr) {
    context.add_table(default_table);
  }

  ExpressionBinder binder(context);

  auto bind_operand = [&](const Expression *src, bool is_attr, const Value &value, const RelAttrSqlNode &attr,
                            unique_ptr<Expression> &out) -> RC {
    unique_ptr<Expression> expr;
    if (src != nullptr) {
      expr = src->copy();
    } else if (is_attr) {
      expr = make_unique<UnboundFieldExpr>(attr.relation_name, attr.attribute_name);
    } else {
      expr = make_unique<ValueExpr>(value);
    }

    vector<unique_ptr<Expression>> bound;
    RC rc = binder.bind_expression(expr, bound);
    if (OB_FAIL(rc)) {
      return rc;
    }
    if (bound.size() != 1) {
      LOG_WARN("condition operand should bind to exactly one expression, got %d", static_cast<int>(bound.size()));
      return RC::INVALID_ARGUMENT;
    }
    out = std::move(bound[0]);
    return RC::SUCCESS;
  };

  unique_ptr<Expression> left_expr;
  RC rc = bind_operand(condition.left_expr, condition.left_is_attr, condition.left_value, condition.left_attr, left_expr);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to bind left operand of condition");
    return rc;
  }

  unique_ptr<Expression> predicate;
  if (condition.subquery != nullptr) {
    unique_ptr<SubqueryExpr> subquery = make_unique<SubqueryExpr>(condition.subquery);
    predicate = make_unique<InExpr>(std::move(left_expr), std::move(subquery), condition.not_in);
  } else if (condition.is_null || condition.is_not_null) {
    predicate = make_unique<IsNullExpr>(std::move(left_expr), condition.is_not_null);
  } else {
    unique_ptr<Expression> right_expr;
    rc = bind_operand(condition.right_expr, condition.right_is_attr, condition.right_value, condition.right_attr, right_expr);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to bind right operand of condition");
      return rc;
    }
    predicate = make_unique<ComparisonExpr>(condition.comp, std::move(left_expr), std::move(right_expr));
  }

  vector<unique_ptr<Expression>> bound_predicate;
  rc = binder.bind_expression(predicate, bound_predicate);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to bind condition expression");
    return rc;
  }
  if (bound_predicate.size() != 1) {
    return RC::INVALID_ARGUMENT;
  }

  filter_unit = new FilterUnit;
  filter_unit->set_expr(std::move(bound_predicate[0]));
  return RC::SUCCESS;
}
