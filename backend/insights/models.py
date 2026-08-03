from django.db import models


class Insight(models.Model):
    date = models.DateField(unique=True, db_index=True)
    ai_summary = models.TextField()
    structured_data_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'insights'
        ordering = ['-date']

    def __str__(self):
        return f"Insight — {self.date}"
