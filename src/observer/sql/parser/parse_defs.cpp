#include "sql/parser/parse_defs.h"

#include "sql/expr/expression.h"

ConditionSqlNode::ConditionSqlNode(const ConditionSqlNode &other)
    : left_is_attr(other.left_is_attr),
      left_value(other.left_value),
      left_attr(other.left_attr),
      comp(other.comp),
      right_is_attr(other.right_is_attr),
      right_attr(other.right_attr),
      right_value(other.right_value),
      left_expr(other.left_expr == nullptr ? nullptr : other.left_expr->copy().release()),
      right_expr(other.right_expr == nullptr ? nullptr : other.right_expr->copy().release()),
      subquery(other.subquery),
      not_in(other.not_in),
      is_null(other.is_null),
      is_not_null(other.is_not_null)
{}

ConditionSqlNode &ConditionSqlNode::operator=(const ConditionSqlNode &other)
{
  if (this == &other) {
    return *this;
  }
  delete left_expr;
  delete right_expr;
  left_is_attr  = other.left_is_attr;
  left_value    = other.left_value;
  left_attr     = other.left_attr;
  comp          = other.comp;
  right_is_attr = other.right_is_attr;
  right_attr    = other.right_attr;
  right_value   = other.right_value;
  left_expr     = other.left_expr == nullptr ? nullptr : other.left_expr->copy().release();
  right_expr    = other.right_expr == nullptr ? nullptr : other.right_expr->copy().release();
  subquery      = other.subquery;
  not_in        = other.not_in;
  is_null       = other.is_null;
  is_not_null   = other.is_not_null;
  return *this;
}

ConditionSqlNode::~ConditionSqlNode()
{
  delete left_expr;
  delete right_expr;
}
