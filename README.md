# Visformer
This repo is to implement convit using MindSpore

## TO DO
1. 跑通多卡训练
2. 提升训练精度
3. pynative+混合编程训练

## 更新日志

### 2022/10/9

多卡训练报错, 报错内容如下

```
Traceback (most recent call last):
  File "train.py", line 191, in <module>
    train(args)
  File "train.py", line 23, in train
    init()
  File "/root/anaconda3/lib/python3.7/site-packages/mindspore/communication/management.py", line 150, in init
    init_gpu_collective()
RuntimeError: Role name '' is invalid. Maybe you are trying to call 'mindspore.communication.init()' without using 'mpirun', which will make MindSpore load several environment variables and check their validation. Please use 'mpirun' to launch this process to fix this issue, or refer to this link if you want to run distributed training without using 'mpirun': https://www.mindspore.cn/docs/programming_guide/zh-CN/r1.6/distributed_training_gpu.html#openmpi.

----------------------------------------------------
- C++ Call Stack: (For framework developers)
----------------------------------------------------
mindspore/ccsrc/distributed/cluster/cluster_context.cc:186 InitNodeRole
```
