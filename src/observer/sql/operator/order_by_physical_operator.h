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

#include "sql/operator/physical_operator.h"

/**
 * @brief 排序物理算子
 * @ingroup PhysicalOperator
 * @details 先把子算子的所有元组物化下来，再按 order by 表达式排序。
 */
class OrderByPhysicalOperator : public PhysicalOperator
{
public:
  OrderByPhysicalOperator(vector<unique_ptr<Expression>> &&expressions, vector<bool> &&asc)
      : expressions_(std::move(expressions)), asc_(std::move(asc))
  {}

  virtual ~OrderByPhysicalOperator() = default;

  PhysicalOperatorType type() const override { return PhysicalOperatorType::ORDER_BY; }
  OpType               get_op_type() const override { return OpType::ORDERBY; }

  RC open(Trx *trx) override;
  RC next() override;
  RC close() override;

  Tuple *current_tuple() override;
  RC     tuple_schema(TupleSchema &schema) const override;

private:
  RC materialize();

private:
  vector<unique_ptr<Expression>> expressions_;
  vector<bool>                   asc_;

  vector<ValueListTuple>         tuples_;
  vector<vector<Value>>          sort_keys_;
  vector<size_t>                 order_;
  size_t                         pos_           = 0;
  size_t                         current_index_ = 0;
  bool                           materialized_  = false;
};
