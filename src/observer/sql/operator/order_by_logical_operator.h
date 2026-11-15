/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MARCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#pragma once

#include "sql/operator/logical_operator.h"

/**
 * @brief 排序逻辑算子
 * @ingroup LogicalOperator
 */
class OrderByLogicalOperator : public LogicalOperator
{
public:
  OrderByLogicalOperator(vector<unique_ptr<Expression>> &&expressions, vector<bool> &&asc)
  {
    expressions_ = std::move(expressions);
    asc_         = std::move(asc);
  }

  virtual ~OrderByLogicalOperator() = default;

  LogicalOperatorType type() const override { return LogicalOperatorType::ORDER_BY; }
  OpType              get_op_type() const override { return OpType::ORDERBY; }

  vector<unique_ptr<Expression>> &order_by_expressions() { return expressions_; }
  vector<bool>                   &asc() { return asc_; }

private:
  vector<bool> asc_;
};
