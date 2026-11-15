#pragma once

#include <string>
#include <cstdio>

/**
 * @brief 将 YYYY-M-D 形式的日期字符串规范化为 YYYY-MM-DD
 * @return 日期合法返回 true，否则返回 false
 */
inline bool normalize_date_string(const std::string &input, std::string &output)
{
  int year = 0, month = 0, day = 0;
  char dash1 = 0, dash2 = 0;
  if (sscanf(input.c_str(), "%d%c%d%c%d", &year, &dash1, &month, &dash2, &day) != 5) {
    return false;
  }
  if (dash1 != '-' || dash2 != '-' || year < 1970 || year > 2038) {
    return false;
  }
  if (month < 1 || month > 12 || day < 1) {
    return false;
  }
  static const int days_in_month[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  int max_day = days_in_month[month - 1];
  if (month == 2 && ((year % 4 == 0 && year % 100 != 0) || year % 400 == 0)) {
    max_day = 29;
  }
  if (day > max_day) {
    return false;
  }
  char buffer[16];
  snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d", year, month, day);
  output = buffer;
  return true;
}
