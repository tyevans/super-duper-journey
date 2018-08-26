# Generated by Django 2.1 on 2018-08-24 21:09

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='PBObjectDetector',
            fields=[
                ('id', models.UUIDField(primary_key=True, serialize=False)),
                ('name', models.TextField()),
                ('pb_file', models.FileField(help_text='frozen_inference_graph.pb', upload_to='data/pb_files')),
                ('label_file', models.FileField(help_text='labels.pbtxt', upload_to='data/label_files')),
                ('active', models.BooleanField(default=True)),
            ],
        ),
    ]
