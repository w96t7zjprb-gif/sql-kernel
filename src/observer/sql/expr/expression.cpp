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
// Created by Wangyunlai on 2022/07/05.
//

#include "sql/expr/expression.h"
#include "sql/expr/tuple.h"
#include "sql/expr/arithmetic_operator.hpp"

using namespace std;

RC FieldExpr::get_value(const Tuple &tuple, Value &value) const
{
  return tuple.find_cell(TupleCellSpec(table_name(), field_name()), value);
}

bool FieldExpr::equal(const Expression &other) const
{
  if (this == &other) {
    return true;
  }
  if (other.type() != ExprType::FIELD) {
    return false;
  }
  const auto &other_field_expr = static_cast<const FieldExpr &>(other);
  return table_name() == other_field_expr.table_name() && field_name() == other_field_expr.field_name();
}

// TODO: 在进行表达式计算时，`chunk` 包含了所有列，因此可以通过 `field_id` 获取到对应列。
// 后续可以优化成在 `FieldExpr` 中存储 `chunk` 中某列的位置信息。
RC FieldExpr::get_column(Chunk &chunk, Column &column)
{
  if (pos_ != -1) {
    column.reference(chunk.column(pos_));
  } else {
    column.reference(chunk.column(field().meta()->field_id()));
  }
  return RC::SUCCESS;
}

bool ValueExpr::equal(const Expression &other) const
{
  if (this == &other) {
    return true;
  }
  if (other.type() != ExprType::VALUE) {
    return false;
  }
  const auto &other_value_expr = static_cast<const ValueExpr &>(other);
  return value_.compare(other_value_expr.get_value()) == 0;
}

RC ValueExpr::get_value(const Tuple &tuple, Value &value) const
{
  value = value_;
  return RC::SUCCESS;
}

RC ValueExpr::get_column(Chunk &chunk, Column &column)
{
  column.init(value_, chunk.rows());
  return RC::SUCCESS;
}

/////////////////////////////////////////////////////////////////////////////////
CastExpr::CastExpr(unique_ptr<Expression> child, AttrType cast_type) : child_(std::move(child)), cast_type_(cast_type)
{}

CastExpr::~CastExpr() {}

RC CastExpr::cast(const Value &value, Value &cast_value) const
{
  RC rc = RC::SUCCESS;
  if (this->value_type() == value.attr_type()) {
    cast_value = value;
    return rc;
  }
  rc = Value::cast_to(value, cast_type_, cast_value);
  return rc;
}

RC CastExpr::get_value(const Tuple &tuple, Value &result) const
{
  Value value;
  RC rc = child_->get_value(tuple, value);
  if (rc != RC::SUCCESS) {
    return rc;
  }

  return cast(value, result);
}

RC CastExpr::get_column(Chunk &chunk, Column &column)
{
  Column child_column;
  RC rc = child_->get_column(chunk, child_column);
  if (rc != RC::SUCCESS) {
    return rc;
  }
  column.init(cast_type_, child_column.attr_len());
  for (int i = 0; i < child_column.count(); ++i) {
    Value value = child_column.get_value(i);
    Value cast_value;
    rc = cast(value, cast_value);
    if (rc != RC::SUCCESS) {
      return rc;
    }
    column.append_value(cast_value);
  }
  return rc;
}

RC CastExpr::try_get_value(Value &result) const
{
  Value value;
  RC rc = child_->try_get_value(value);
  if (rc != RC::SUCCESS) {
    return rc;
  }

  return cast(value, result);
}

////////////////////////////////////////////////////////////////////////////////

ComparisonExpr::ComparisonExpr(CompOp comp, unique_ptr<Expression> left, unique_ptr<Expression> right)
    : comp_(comp), left_(std::move(left)), right_(std::move(right))
{
}

ComparisonExpr::~ComparisonExpr() {}

RC ComparisonExpr::compare_value(const Value &left, const Value &right, bool &result) const
{
  RC  rc         = RC::SUCCESS;
  result         = false;
  if (left.is_null() || right.is_null()) {
    return RC::SUCCESS;
  }
  int cmp_result = left.compare(right);
  switch (comp_) {
    case EQUAL_TO: {
      result = (0 == cmp_result);
    } break;
    case LESS_EQUAL: {
      result = (cmp_result <= 0);
    } break;
    case NOT_EQUAL: {
      result = (cmp_result != 0);
    } break;
    case LESS_THAN: {
      result = (cmp_result < 0);
    } break;
    case GREAT_EQUAL: {
      result = (cmp_result >= 0);
    } break;
    case GREAT_THAN: {
      result = (cmp_result > 0);
    } break;
    default: {
      LOG_WARN("unsupported comparison. %d", comp_);
      rc = RC::INTERNAL;
    } break;
  }

  return rc;
}

RC ComparisonExpr::try_get_value(Value &cell) const
{
  if (left_->type() == ExprType::VALUE && right_->type() == ExprType::VALUE) {
    ValueExpr *  left_value_expr  = static_cast<ValueExpr *>(left_.get());
    ValueExpr *  right_value_expr = static_cast<ValueExpr *>(right_.get());
    const Value &left_cell        = left_value_expr->get_value();
    const Value &right_cell       = right_value_expr->get_value();

    bool value = false;
    RC   rc    = compare_value(left_cell, right_cell, value);
    if (rc != RC::SUCCESS) {
      LOG_WARN("failed to compare tuple cells. rc=%s", strrc(rc));
    } else {
      cell.set_boolean(value);
    }
    return rc;
  }

  return RC::INVALID_ARGUMENT;
}

RC ComparisonExpr::get_value(const Tuple &tuple, Value &value) const
{
  Value left_value;
  Value right_value;

  RC rc = left_->get_value(tuple, left_value);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of left expression. rc=%s", strrc(rc));
    return rc;
  }
  rc = right_->get_value(tuple, right_value);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of right expression. rc=%s", strrc(rc));
    return rc;
  }

  bool bool_value = false;

  rc = compare_value(left_value, right_value, bool_value);
  if (rc == RC::SUCCESS) {
    value.set_boolean(bool_value);
  }
  return rc;
}

RC ComparisonExpr::eval(Chunk &chunk, vector<uint8_t> &select)
{
  RC     rc = RC::SUCCESS;
  Column left_column;
  Column right_column;

  rc = left_->get_column(chunk, left_column);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of left expression. rc=%s", strrc(rc));
    return rc;
  }
  rc = right_->get_column(chunk, right_column);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of right expression. rc=%s", strrc(rc));
    return rc;
  }
  if (left_column.attr_type() != right_column.attr_type()) {
    LOG_WARN("cannot compare columns with different types");
    return RC::INTERNAL;
  }
  if (left_column.attr_type() == AttrType::INTS) {
    rc = compare_column<int>(left_column, right_column, select);
  } else if (left_column.attr_type() == AttrType::FLOATS) {
    rc = compare_column<float>(left_column, right_column, select);
  } else if (left_column.attr_type() == AttrType::CHARS) {
    int rows = 0;
    if (left_column.column_type() == Column::Type::CONSTANT_COLUMN) {
      rows = right_column.count();
    } else {
      rows = left_column.count();
    }
    for (int i = 0; i < rows; ++i) {
      Value left_val = left_column.get_value(i);
      Value right_val = right_column.get_value(i);
      bool        result   = false;
      rc                   = compare_value(left_val, right_val, result);
      if (rc != RC::SUCCESS) {
        LOG_WARN("failed to compare tuple cells. rc=%s", strrc(rc));
        return rc;
      }
      select[i] &= result ? 1 : 0;
    }

  } else {
    LOG_WARN("unsupported data type %d", left_column.attr_type());
    return RC::INTERNAL;
  }
  return rc;
}

template <typename T>
RC ComparisonExpr::compare_column(const Column &left, const Column &right, vector<uint8_t> &result) const
{
  RC rc = RC::SUCCESS;

  bool left_const  = left.column_type() == Column::Type::CONSTANT_COLUMN;
  bool right_const = right.column_type() == Column::Type::CONSTANT_COLUMN;
  if (left_const && right_const) {
    compare_result<T, true, true>((T *)left.data(), (T *)right.data(), left.count(), result, comp_);
  } else if (left_const && !right_const) {
    compare_result<T, true, false>((T *)left.data(), (T *)right.data(), right.count(), result, comp_);
  } else if (!left_const && right_const) {
    compare_result<T, false, true>((T *)left.data(), (T *)right.data(), left.count(), result, comp_);
  } else {
    compare_result<T, false, false>((T *)left.data(), (T *)right.data(), left.count(), result, comp_);
  }
  return rc;
}

////////////////////////////////////////////////////////////////////////////////
ConjunctionExpr::ConjunctionExpr(Type type, vector<unique_ptr<Expression>> &children)
    : conjunction_type_(type), children_(std::move(children))
{}

RC ConjunctionExpr::get_value(const Tuple &tuple, Value &value) const
{
  RC rc = RC::SUCCESS;
  if (children_.empty()) {
    value.set_boolean(true);
    return rc;
  }

  Value tmp_value;
  for (const unique_ptr<Expression> &expr : children_) {
    rc = expr->get_value(tuple, tmp_value);
    if (rc != RC::SUCCESS) {
      LOG_WARN("failed to get value by child expression. rc=%s", strrc(rc));
      return rc;
    }
    bool bool_value = tmp_value.get_boolean();
    if ((conjunction_type_ == Type::AND && !bool_value) || (conjunction_type_ == Type::OR && bool_value)) {
      value.set_boolean(bool_value);
      return rc;
    }
  }

  bool default_value = (conjunction_type_ == Type::AND);
  value.set_boolean(default_value);
  return rc;
}

////////////////////////////////////////////////////////////////////////////////

ArithmeticExpr::ArithmeticExpr(ArithmeticExpr::Type type, Expression *left, Expression *right)
    : arithmetic_type_(type), left_(left), right_(right)
{}
ArithmeticExpr::ArithmeticExpr(ArithmeticExpr::Type type, unique_ptr<Expression> left, unique_ptr<Expression> right)
    : arithmetic_type_(type), left_(std::move(left)), right_(std::move(right))
{}

bool ArithmeticExpr::equal(const Expression &other) const
{
  if (this == &other) {
    return true;
  }
  if (type() != other.type()) {
    return false;
  }
  auto &other_arith_expr = static_cast<const ArithmeticExpr &>(other);
  return arithmetic_type_ == other_arith_expr.arithmetic_type() && left_->equal(*other_arith_expr.left_) &&
         right_->equal(*other_arith_expr.right_);
}
AttrType ArithmeticExpr::value_type() const
{
  if (!right_) {
    return left_->value_type();
  }

  if ((left_->value_type() == AttrType::INTS) &&
   (right_->value_type() == AttrType::INTS) &&
      arithmetic_type_ != Type::DIV) {
    return AttrType::INTS;
  }

  return AttrType::FLOATS;
}

RC ArithmeticExpr::calc_value(const Value &left_value, const Value &right_value, Value &value) const
{
  RC rc = RC::SUCCESS;

  const AttrType target_type = value_type();
  value.set_type(target_type);

  switch (arithmetic_type_) {
    case Type::ADD: {
      Value::add(left_value, right_value, value);
    } break;

    case Type::SUB: {
      Value::subtract(left_value, right_value, value);
    } break;

    case Type::MUL: {
      Value::multiply(left_value, right_value, value);
    } break;

    case Type::DIV: {
      Value::divide(left_value, right_value, value);
    } break;

    case Type::NEGATIVE: {
      Value::negative(left_value, value);
    } break;

    default: {
      rc = RC::INTERNAL;
      LOG_WARN("unsupported arithmetic type. %d", arithmetic_type_);
    } break;
  }
  return rc;
}

template <bool LEFT_CONSTANT, bool RIGHT_CONSTANT>
RC ArithmeticExpr::execute_calc(
    const Column &left, const Column &right, Column &result, Type type, AttrType attr_type) const
{
  RC rc = RC::SUCCESS;
  switch (type) {
    case Type::ADD: {
      if (attr_type == AttrType::INTS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, int, AddOperator>(
            (int *)left.data(), (int *)right.data(), (int *)result.data(), result.capacity());
      } else if (attr_type == AttrType::FLOATS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, float, AddOperator>(
            (float *)left.data(), (float *)right.data(), (float *)result.data(), result.capacity());
      } else {
        rc = RC::UNIMPLEMENTED;
      }
    } break;
    case Type::SUB:
      if (attr_type == AttrType::INTS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, int, SubtractOperator>(
            (int *)left.data(), (int *)right.data(), (int *)result.data(), result.capacity());
      } else if (attr_type == AttrType::FLOATS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, float, SubtractOperator>(
            (float *)left.data(), (float *)right.data(), (float *)result.data(), result.capacity());
      } else {
        rc = RC::UNIMPLEMENTED;
      }
      break;
    case Type::MUL:
      if (attr_type == AttrType::INTS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, int, MultiplyOperator>(
            (int *)left.data(), (int *)right.data(), (int *)result.data(), result.capacity());
      } else if (attr_type == AttrType::FLOATS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, float, MultiplyOperator>(
            (float *)left.data(), (float *)right.data(), (float *)result.data(), result.capacity());
      } else {
        rc = RC::UNIMPLEMENTED;
      }
      break;
    case Type::DIV:
      if (attr_type == AttrType::INTS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, int, DivideOperator>(
            (int *)left.data(), (int *)right.data(), (int *)result.data(), result.capacity());
      } else if (attr_type == AttrType::FLOATS) {
        binary_operator<LEFT_CONSTANT, RIGHT_CONSTANT, float, DivideOperator>(
            (float *)left.data(), (float *)right.data(), (float *)result.data(), result.capacity());
      } else {
        rc = RC::UNIMPLEMENTED;
      }
      break;
    case Type::NEGATIVE:
      if (attr_type == AttrType::INTS) {
        unary_operator<LEFT_CONSTANT, int, NegateOperator>((int *)left.data(), (int *)result.data(), result.capacity());
      } else if (attr_type == AttrType::FLOATS) {
        unary_operator<LEFT_CONSTANT, float, NegateOperator>(
            (float *)left.data(), (float *)result.data(), result.capacity());
      } else {
        rc = RC::UNIMPLEMENTED;
      }
      break;
    default: rc = RC::UNIMPLEMENTED; break;
  }
  if (rc == RC::SUCCESS) {
    result.set_count(result.capacity());
  }
  return rc;
}

RC ArithmeticExpr::get_value(const Tuple &tuple, Value &value) const
{
  RC rc = RC::SUCCESS;

  Value left_value;
  Value right_value;

  rc = left_->get_value(tuple, left_value);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of left expression. rc=%s", strrc(rc));
    return rc;
  }
  if (right_ != nullptr) {
    rc = right_->get_value(tuple, right_value);
    if (rc != RC::SUCCESS) {
      LOG_WARN("failed to get value of right expression. rc=%s", strrc(rc));
      return rc;
    }
  }
  if (left_value.is_null() || (right_ != nullptr && right_value.is_null())) {
    value.set_null(value_type());
    return RC::SUCCESS;
  }
  return calc_value(left_value, right_value, value);
}

RC ArithmeticExpr::get_column(Chunk &chunk, Column &column)
{
  RC rc = RC::SUCCESS;
  if (pos_ != -1) {
    column.reference(chunk.column(pos_));
    return rc;
  }
  Column left_column;
  Column right_column;

  rc = left_->get_column(chunk, left_column);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get column of left expression. rc=%s", strrc(rc));
    return rc;
  }
  if (right_ != nullptr) {
    rc = right_->get_column(chunk, right_column);
    if (rc != RC::SUCCESS) {
      LOG_WARN("failed to get column of right expression. rc=%s", strrc(rc));
      return rc;
    }
  }
  return calc_column(left_column, right_column, column);
}

RC ArithmeticExpr::calc_column(const Column &left_column, const Column &right_column, Column &column) const
{
  RC rc = RC::SUCCESS;

  const AttrType target_type = value_type();
  column.init(target_type, left_column.attr_len(), max(left_column.count(), right_column.count()));
  bool left_const  = left_column.column_type() == Column::Type::CONSTANT_COLUMN;
  bool right_const = right_column.column_type() == Column::Type::CONSTANT_COLUMN;
  if (left_const && right_const) {
    column.set_column_type(Column::Type::CONSTANT_COLUMN);
    rc = execute_calc<true, true>(left_column, right_column, column, arithmetic_type_, target_type);
  } else if (left_const && !right_const) {
    column.set_column_type(Column::Type::NORMAL_COLUMN);
    rc = execute_calc<true, false>(left_column, right_column, column, arithmetic_type_, target_type);
  } else if (!left_const && right_const) {
    column.set_column_type(Column::Type::NORMAL_COLUMN);
    rc = execute_calc<false, true>(left_column, right_column, column, arithmetic_type_, target_type);
  } else {
    column.set_column_type(Column::Type::NORMAL_COLUMN);
    rc = execute_calc<false, false>(left_column, right_column, column, arithmetic_type_, target_type);
  }
  return rc;
}

RC ArithmeticExpr::try_get_value(Value &value) const
{
  RC rc = RC::SUCCESS;

  Value left_value;
  Value right_value;

  rc = left_->try_get_value(left_value);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to get value of left expression. rc=%s", strrc(rc));
    return rc;
  }

  if (right_) {
    rc = right_->try_get_value(right_value);
    if (rc != RC::SUCCESS) {
      LOG_WARN("failed to get value of right expression. rc=%s", strrc(rc));
      return rc;
    }
  }

  return calc_value(left_value, right_value, value);
}

////////////////////////////////////////////////////////////////////////////////

UnboundAggregateExpr::UnboundAggregateExpr(const char *aggregate_name, Expression *child, int arg_count)
    : aggregate_name_(aggregate_name), child_(child), arg_count_(arg_count)
{}

UnboundAggregateExpr::UnboundAggregateExpr(const char *aggregate_name, unique_ptr<Expression> child, int arg_count)
    : aggregate_name_(aggregate_name), child_(std::move(child)), arg_count_(arg_count)
{}

////////////////////////////////////////////////////////////////////////////////
AggregateExpr::AggregateExpr(Type type, Expression *child) : aggregate_type_(type), child_(child) {}

AggregateExpr::AggregateExpr(Type type, unique_ptr<Expression> child) : aggregate_type_(type), child_(std::move(child))
{}

RC AggregateExpr::get_column(Chunk &chunk, Column &column)
{
  RC rc = RC::SUCCESS;
  if (pos_ != -1) {
    column.reference(chunk.column(pos_));
  } else {
    rc = RC::INTERNAL;
  }
  return rc;
}

bool AggregateExpr::equal(const Expression &other) const
{
  if (this == &other) {
    return true;
  }
  if (other.type() != type()) {
    return false;
  }
  const AggregateExpr &other_aggr_expr = static_cast<const AggregateExpr &>(other);
  return aggregate_type_ == other_aggr_expr.aggregate_type() && child_->equal(*other_aggr_expr.child());
}

unique_ptr<Aggregator> AggregateExpr::create_aggregator() const
{
  unique_ptr<Aggregator> aggregator;
  switch (aggregate_type_) {
    case Type::SUM: {
      aggregator = make_unique<SumAggregator>();
      break;
    }
    case Type::COUNT: {
      aggregator = make_unique<CountAggregator>();
      break;
    }
    case Type::AVG: {
      aggregator = make_unique<AvgAggregator>();
      break;
    }
    case Type::MAX: {
      aggregator = make_unique<MaxAggregator>();
      break;
    }
    case Type::MIN: {
      aggregator = make_unique<MinAggregator>();
      break;
    }
    default: {
      ASSERT(false, "unsupported aggregate type");
      break;
    }
  }
  return aggregator;
}

RC AggregateExpr::get_value(const Tuple &tuple, Value &value) const
{
  return tuple.find_cell(TupleCellSpec(name()), value);
}

RC AggregateExpr::type_from_string(const char *type_str, AggregateExpr::Type &type)
{
  RC rc = RC::SUCCESS;
  if (0 == strcasecmp(type_str, "count")) {
    type = Type::COUNT;
  } else if (0 == strcasecmp(type_str, "sum")) {
    type = Type::SUM;
  } else if (0 == strcasecmp(type_str, "avg")) {
    type = Type::AVG;
  } else if (0 == strcasecmp(type_str, "max")) {
    type = Type::MAX;
  } else if (0 == strcasecmp(type_str, "min")) {
    type = Type::MIN;
  } else {
    rc = RC::INVALID_ARGUMENT;
  }
  return rc;
}

////////////////////////////////////////////////////////////////////////////////
// 子查询相关表达式

#include "sql/stmt/select_stmt.h"
#include "sql/stmt/stmt.h"
#include "sql/optimizer/logical_plan_generator.h"
#include "sql/optimizer/physical_plan_generator.h"
#include "sql/operator/logical_operator.h"
#include "sql/operator/physical_operator.h"
#include "storage/db/db.h"
#include "storage/trx/trx.h"
#include "storage/table/table.h"

namespace {
thread_local Trx *g_subquery_trx = nullptr;
thread_local vector<Table *> g_binding_outer_tables;
}

void set_subquery_trx(Trx *trx) { g_subquery_trx = trx; }

void set_subquery_binding_outer_tables(const vector<Table *> &tables) { g_binding_outer_tables = tables; }
const vector<Table *> &subquery_binding_outer_tables() { return g_binding_outer_tables; }

static SelectSqlNode clone_select_node(const SelectSqlNode &src);
static ConditionSqlNode clone_condition_node(const ConditionSqlNode &src);

static ConditionSqlNode clone_condition_node(const ConditionSqlNode &src)
{
  ConditionSqlNode dst(src);
  if (src.subquery) {
    dst.subquery = make_shared<SelectSqlNode>(clone_select_node(*src.subquery));
  }
  return dst;
}

static SelectSqlNode clone_select_node(const SelectSqlNode &src)
{
  SelectSqlNode dst;
  for (const auto &expr : src.expressions) {
    dst.expressions.emplace_back(expr->copy());
  }
  dst.relations = src.relations;
  for (const auto &condition : src.conditions) {
    dst.conditions.emplace_back(clone_condition_node(condition));
  }
  for (const auto &expr : src.group_by) {
    dst.group_by.emplace_back(expr->copy());
  }
  for (const auto &item : src.order_by) {
    SelectSqlNode::OrderByItem new_item;
    new_item.expression = item.expression->copy();
    new_item.asc        = item.asc;
    dst.order_by.emplace_back(std::move(new_item));
  }
  return dst;
}

static bool table_name_in_list(const char *name, const vector<Table *> &tables)
{
  if (name == nullptr || name[0] == '\0') {
    return false;
  }
  for (Table *table : tables) {
    if (0 == strcasecmp(name, table->name())) {
      return true;
    }
  }
  return false;
}

static void replace_outer_expr(unique_ptr<Expression> &expr, const Tuple &tuple, const vector<Table *> &outer_tables);

static void replace_outer_raw_expr(Expression *&expr, const Tuple &tuple, const vector<Table *> &outer_tables)
{
  unique_ptr<Expression> holder(expr);
  replace_outer_expr(holder, tuple, outer_tables);
  expr = holder.release();
}

static void replace_outer_condition(ConditionSqlNode &condition, const Tuple &tuple, const vector<Table *> &outer_tables)
{
  if (condition.left_expr != nullptr) {
    replace_outer_raw_expr(condition.left_expr, tuple, outer_tables);
  }
  if (condition.right_expr != nullptr) {
    replace_outer_raw_expr(condition.right_expr, tuple, outer_tables);
  }
  if (condition.subquery) {
    for (ConditionSqlNode &nested : condition.subquery->conditions) {
      replace_outer_condition(nested, tuple, outer_tables);
    }
    for (auto &expr : condition.subquery->expressions) {
      replace_outer_expr(expr, tuple, outer_tables);
    }
  }
}

static void replace_outer_expr(unique_ptr<Expression> &expr, const Tuple &tuple, const vector<Table *> &outer_tables)
{
  if (expr == nullptr) {
    return;
  }
  switch (expr->type()) {
    case ExprType::UNBOUND_FIELD: {
      auto unbound = static_cast<UnboundFieldExpr *>(expr.get());
      if (table_name_in_list(unbound->table_name(), outer_tables)) {
        for (Table *table : outer_tables) {
          if (0 != strcasecmp(unbound->table_name(), table->name())) {
            continue;
          }
          const FieldMeta *field_meta = table->table_meta().field(unbound->field_name());
          if (field_meta == nullptr) {
            continue;
          }
          FieldExpr field_expr(table, field_meta);
          Value     value;
          RC        rc = field_expr.get_value(tuple, value);
          if (rc == RC::SUCCESS) {
            expr = make_unique<ValueExpr>(value);
          }
          return;
        }
      }
    } break;
    case ExprType::COMPARISON: {
      auto cmp = static_cast<ComparisonExpr *>(expr.get());
      replace_outer_expr(cmp->left(), tuple, outer_tables);
      replace_outer_expr(cmp->right(), tuple, outer_tables);
    } break;
    case ExprType::CONJUNCTION: {
      auto conjunction = static_cast<ConjunctionExpr *>(expr.get());
      for (auto &child : conjunction->children()) {
        replace_outer_expr(child, tuple, outer_tables);
      }
    } break;
    case ExprType::ARITHMETIC: {
      auto arith = static_cast<ArithmeticExpr *>(expr.get());
      replace_outer_expr(arith->left(), tuple, outer_tables);
      if (arith->right()) {
        replace_outer_expr(arith->right(), tuple, outer_tables);
      }
    } break;
    case ExprType::CAST: {
      auto cast = static_cast<CastExpr *>(expr.get());
      replace_outer_expr(cast->child(), tuple, outer_tables);
    } break;
    case ExprType::AGGREGATION: {
      auto aggregate = static_cast<AggregateExpr *>(expr.get());
      replace_outer_expr(aggregate->child(), tuple, outer_tables);
    } break;
    case ExprType::UNBOUND_AGGREGATION: {
      auto aggregate = static_cast<UnboundAggregateExpr *>(expr.get());
      replace_outer_expr(aggregate->child(), tuple, outer_tables);
    } break;
    case ExprType::IN: {
      auto in_expr = static_cast<InExpr *>(expr.get());
      replace_outer_expr(in_expr->left(), tuple, outer_tables);
    } break;
    case ExprType::IS_NULL: {
      auto is_null = static_cast<IsNullExpr *>(expr.get());
      replace_outer_expr(is_null->child(), tuple, outer_tables);
    } break;
    default: break;
  }
}

SubqueryExpr::SubqueryExpr(shared_ptr<SelectSqlNode> subquery) : subquery_(std::move(subquery)) {}

SubqueryExpr::~SubqueryExpr() = default;

unique_ptr<Expression> SubqueryExpr::copy() const
{
  auto expr = make_unique<SubqueryExpr>(subquery_);
  expr->db_ = db_;
  expr->outer_tables_ = outer_tables_;
  return expr;
}

RC SubqueryExpr::prepare_select(const Tuple &tuple, shared_ptr<SelectSqlNode> &out) const
{
  if (subquery_ == nullptr) {
    return RC::INVALID_ARGUMENT;
  }

  SelectSqlNode cloned = clone_select_node(*subquery_);
  for (ConditionSqlNode &condition : cloned.conditions) {
    replace_outer_condition(condition, tuple, outer_tables_);
  }
  for (auto &expr : cloned.expressions) {
    replace_outer_expr(expr, tuple, outer_tables_);
  }
  for (auto &expr : cloned.group_by) {
    replace_outer_expr(expr, tuple, outer_tables_);
  }
  for (auto &item : cloned.order_by) {
    replace_outer_expr(item.expression, tuple, outer_tables_);
  }
  out = make_shared<SelectSqlNode>(std::move(cloned));
  return RC::SUCCESS;
}

RC SubqueryExpr::execute(const Tuple &tuple, vector<Value> &values) const
{
  if (db_ == nullptr) {
    return RC::INVALID_ARGUMENT;
  }
  if (g_subquery_trx == nullptr) {
    LOG_WARN("subquery has no transaction");
    return RC::INTERNAL;
  }

  shared_ptr<SelectSqlNode> prepared;
  RC rc = prepare_select(tuple, prepared);
  if (OB_FAIL(rc)) {
    return rc;
  }

  Stmt *stmt = nullptr;
  vector<Table *> saved_outer = g_binding_outer_tables;
  g_binding_outer_tables = outer_tables_;
  rc = SelectStmt::create(db_, *prepared, stmt);
  g_binding_outer_tables = saved_outer;
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to create subquery select stmt. rc=%s", strrc(rc));
    return rc;
  }
  unique_ptr<Stmt> stmt_owner(stmt);

  SelectStmt *select_stmt = static_cast<SelectStmt *>(stmt);
  if (select_stmt->query_expressions().size() != 1) {
    LOG_WARN("subquery must return exactly one column");
    return RC::INVALID_ARGUMENT;
  }

  LogicalPlanGenerator logical_generator;
  unique_ptr<LogicalOperator> logical_operator;
  rc = logical_generator.create(stmt, logical_operator);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to create subquery logical plan. rc=%s", strrc(rc));
    return rc;
  }

  PhysicalPlanGenerator physical_generator;
  unique_ptr<PhysicalOperator> physical_operator;
  rc = physical_generator.create(*logical_operator, physical_operator, nullptr);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to create subquery physical plan. rc=%s", strrc(rc));
    return rc;
  }

  rc = physical_operator->open(g_subquery_trx);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to open subquery operator. rc=%s", strrc(rc));
    return rc;
  }

  while (OB_SUCC(rc = physical_operator->next())) {
    Tuple *tuple_ptr = physical_operator->current_tuple();
    if (tuple_ptr == nullptr) {
      rc = RC::INTERNAL;
      break;
    }
    Value value;
    rc = tuple_ptr->cell_at(0, value);
    if (OB_FAIL(rc)) {
      break;
    }
    values.emplace_back(value);
  }
  if (rc == RC::RECORD_EOF) {
    rc = RC::SUCCESS;
  }
  physical_operator->close();
  return rc;
}

RC SubqueryExpr::get_values(const Tuple &tuple, vector<Value> &values) const { return execute(tuple, values); }

RC SubqueryExpr::get_value(const Tuple &tuple, Value &value) const
{
  vector<Value> values;
  RC rc = execute(tuple, values);
  if (OB_FAIL(rc)) {
    return rc;
  }
  if (values.empty()) {
    value.set_null();
    return RC::SUCCESS;
  }
  if (values.size() != 1) {
    LOG_WARN("scalar subquery returned %d rows", static_cast<int>(values.size()));
    return RC::INVALID_ARGUMENT;
  }
  value = values.front();
  return RC::SUCCESS;
}

InExpr::InExpr(unique_ptr<Expression> left, unique_ptr<SubqueryExpr> subquery, bool not_in)
    : left_(std::move(left)), subquery_(std::move(subquery)), not_in_(not_in)
{}

InExpr::~InExpr() = default;

unique_ptr<Expression> InExpr::copy() const
{
  return make_unique<InExpr>(left_->copy(), unique_ptr<SubqueryExpr>(static_cast<SubqueryExpr *>(subquery_->copy().release())), not_in_);
}

RC InExpr::get_value(const Tuple &tuple, Value &value) const
{
  Value left_value;
  RC rc = left_->get_value(tuple, left_value);
  if (OB_FAIL(rc)) {
    return rc;
  }

  vector<Value> values;
  rc = subquery_->get_values(tuple, values);
  if (OB_FAIL(rc)) {
    return rc;
  }

  bool matched = false;
  bool has_null = false;
  for (const Value &item : values) {
    if (item.is_null()) {
      has_null = true;
      continue;
    }
    if (!left_value.is_null() && left_value.compare(item) == 0) {
      matched = true;
      break;
    }
  }

  bool result = false;
  if (left_value.is_null()) {
    result = false;
  } else if (matched) {
    result = !not_in_;
  } else if (has_null) {
    result = false;
  } else {
    result = not_in_;
  }
  value.set_boolean(result);
  return RC::SUCCESS;
}

IsNullExpr::IsNullExpr(unique_ptr<Expression> child, bool is_not_null)
    : child_(std::move(child)), is_not_null_(is_not_null)
{}

unique_ptr<Expression> IsNullExpr::copy() const { return make_unique<IsNullExpr>(child_->copy(), is_not_null_); }

RC IsNullExpr::get_value(const Tuple &tuple, Value &value) const
{
  Value child_value;
  RC rc = child_->get_value(tuple, child_value);
  if (OB_FAIL(rc)) {
    return rc;
  }
  bool is_null = child_value.is_null();
  value.set_boolean(is_not_null_ ? !is_null : is_null);
  return RC::SUCCESS;
}
