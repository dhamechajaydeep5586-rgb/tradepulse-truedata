from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0010_signalhistory_whatsapp_flags'),
    ]

    operations = [
        migrations.CreateModel(
            name='IndexConstituent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('index_name', models.CharField(db_index=True, max_length=50)),
                ('symbol', models.CharField(db_index=True, max_length=30)),
                ('company_name', models.CharField(blank=True, max_length=255)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('last_refreshed_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'index_constituents',
                'ordering': ['index_name', 'symbol'],
                'unique_together': {('index_name', 'symbol')},
            },
        ),
    ]
