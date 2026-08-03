# Generated manually — drops Dow Jones/Nasdaq fields (Yahoo Finance removed;
# Angel One has no US-market coverage to replace them with).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('global_market', '0003_globalmarket_updated_at'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='globalmarket',
            name='dow_jones_ltp',
        ),
        migrations.RemoveField(
            model_name='globalmarket',
            name='dow_jones_change',
        ),
        migrations.RemoveField(
            model_name='globalmarket',
            name='nasdaq_ltp',
        ),
        migrations.RemoveField(
            model_name='globalmarket',
            name='nasdaq_change',
        ),
    ]
