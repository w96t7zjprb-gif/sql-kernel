#include "sql/operator/update_physical_operator.h"

#include "common/log/log.h"
#include "storage/table/table.h"
#include "storage/trx/trx.h"

using namespace std;

/**
 * @brief 执行更新
 *
 * 先收集所有需要更新的记录，再统一执行更新操作。如果一边扫描一边更新，
 * 更新后的记录可能会再次被扫描到，从而导致重复更新。
 * 收集记录时扫描器还没有关闭，记录的数据指针指向页面的缓冲区，
 * 因此这里对旧记录做一次深拷贝，避免扫描器关闭后数据失效。
 */
RC UpdatePhysicalOperator::open(Trx *trx)
{
  if (children_.empty()) {
    return RC::SUCCESS;
  }

  RC rc = children_[0]->open(trx);
  if (OB_FAIL(rc)) {
    LOG_WARN("failed to open child operator. rc=%s", strrc(rc));
    return rc;
  }

  const TableMeta &table_meta       = table_->table_meta();
  const int        sys_field_num    = table_meta.sys_field_num();
  const int        normal_field_num = table_meta.field_num() - sys_field_num;

  vector<pair<Record, Record>> updates;
  while (OB_SUCC(rc = children_[0]->next())) {
    Tuple *tuple = children_[0]->current_tuple();
    if (tuple == nullptr) {
      rc = RC::INTERNAL;
      LOG_WARN("failed to get current tuple. table=%s", table_->name());
      break;
    }

    RowTuple *row_tuple   = static_cast<RowTuple *>(tuple);
    Record   &scan_record = row_tuple->record();

    // 深拷贝扫描到的旧记录，扫描器关闭后原数据可能会被页面缓存复用
    Record old_record;
    rc = old_record.copy_data(scan_record.data(), table_meta.record_size());
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to copy record data. table=%s, rc=%s", table_->name(), strrc(rc));
      break;
    }
    old_record.set_rid(scan_record.rid());

    // 用旧记录的所有普通字段值构造新记录，待更新字段替换成新的值
    vector<Value> values;
    values.reserve(normal_field_num);
    for (int i = 0; i < normal_field_num; i++) {
      const FieldMeta *field = table_meta.field(i + sys_field_num);

      Value value;
      rc = tuple->cell_at(i + sys_field_num, value);
      if (OB_FAIL(rc)) {
        LOG_WARN("failed to get cell value. table=%s, field=%s, rc=%s",
            table_->name(), field->name(), strrc(rc));
        break;
      }

      if (0 == strcmp(field->name(), field_meta_->name())) {
        value = value_;
      }
      values.push_back(value);
    }
    if (OB_FAIL(rc)) {
      break;
    }

    Record new_record;
    rc = table_->make_record(normal_field_num, values.data(), new_record);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to make new record. table=%s, rc=%s", table_->name(), strrc(rc));
      break;
    }

    // 系统字段（如事务信息）沿用旧记录，避免更新记录时被清空
    if (sys_field_num > 0) {
      const FieldMeta *last_sys_field  = table_meta.field(sys_field_num - 1);
      const int        sys_field_bytes = last_sys_field->offset() + last_sys_field->len();
      memcpy(new_record.data(), old_record.data(), sys_field_bytes);
    }

    updates.emplace_back(std::move(old_record), std::move(new_record));
  }

  children_[0]->close();
  if (rc != RC::RECORD_EOF) {
    return rc;
  }

  // 记录的有效性以及索引的维护由存储引擎来保证
  for (auto &update : updates) {
    rc = table_->update_record_with_trx(update.first, update.second, trx);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to update record. table=%s, rc=%s", table_->name(), strrc(rc));
      return rc;
    }
  }
  return RC::SUCCESS;
}

RC UpdatePhysicalOperator::next()
{
  return RC::RECORD_EOF;
}

RC UpdatePhysicalOperator::close()
{
  return RC::SUCCESS;
}
