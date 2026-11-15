/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MARCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#include "sql/operator/order_by_physical_operator.h"

#include <algorithm>
#include <numeric>

#include "common/log/log.h"

using namespace std;

RC OrderByPhysicalOperator::open(Trx *trx)
{
  if (children_.empty()) {
    return RC::SUCCESS;
  }
  return children_[0]->open(trx);
}

RC OrderByPhysicalOperator::materialize()
{
  if (materialized_) {
    return RC::SUCCESS;
  }
  materialized_ = true;

  if (children_.empty()) {
    return RC::SUCCESS;
  }

  PhysicalOperator *child = children_[0].get();
  RC                rc    = RC::SUCCESS;
  while (OB_SUCC(rc = child->next())) {
    Tuple *tuple = child->current_tuple();
    if (nullptr == tuple) {
      return RC::INTERNAL;
    }

    ValueListTuple value_list;
    rc = ValueListTuple::make(*tuple, value_list);
    if (OB_FAIL(rc)) {
      return rc;
    }

    vector<Value> keys;
    keys.reserve(expressions_.size());
    for (auto &expression : expressions_) {
      Value value;
      rc = expression->get_value(*tuple, value);
      if (OB_FAIL(rc)) {
        return rc;
      }
      keys.push_back(value);
    }

    tuples_.push_back(std::move(value_list));
    sort_keys_.push_back(std::move(keys));
  }

  if (rc != RC::RECORD_EOF) {
    return rc;
  }

  order_.resize(tuples_.size());
  std::iota(order_.begin(), order_.end(), 0);

  std::stable_sort(order_.begin(), order_.end(), [this](size_t left, size_t right) {
    const vector<Value> &left_keys  = sort_keys_[left];
    const vector<Value> &right_keys = sort_keys_[right];
    for (size_t i = 0; i < left_keys.size(); i++) {
      int cmp = left_keys[i].compare(right_keys[i]);
      if (cmp != 0) {
        return asc_[i] ? cmp < 0 : cmp > 0;
      }
    }
    return false;
  });

  return RC::SUCCESS;
}

RC OrderByPhysicalOperator::next()
{
  RC rc = materialize();
  if (OB_FAIL(rc)) {
    return rc;
  }

  if (pos_ >= order_.size()) {
    return RC::RECORD_EOF;
  }

  current_index_ = order_[pos_];
  pos_++;
  return RC::SUCCESS;
}

RC OrderByPhysicalOperator::close()
{
  if (!children_.empty()) {
    children_[0]->close();
  }
  tuples_.clear();
  sort_keys_.clear();
  order_.clear();
  pos_          = 0;
  materialized_ = false;
  return RC::SUCCESS;
}

Tuple *OrderByPhysicalOperator::current_tuple()
{
  if (current_index_ >= tuples_.size()) {
    return nullptr;
  }
  return &tuples_[current_index_];
}

RC OrderByPhysicalOperator::tuple_schema(TupleSchema &schema) const
{
  if (children_.empty()) {
    return RC::SUCCESS;
  }
  return children_[0]->tuple_schema(schema);
}
