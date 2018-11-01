### Ideas

* train separate models for small number of categories
  * use to decide upon "ambiguous" results
* break down precision per
  * category
  * recognized vs. non-recognized
* visualize confusion matrix
  * which categories are usually confused?
  * train separate models for categories which are often confused?
* ignore samples with recognized=false
* check how close the top-3 predictions softmax is for wrongly classified samples
* normalize batch
* mmap numpy images
* https://www.kaggle.com/c/quickdraw-doodle-recognition/discussion/68006
* at batch normalization as first layer in resnet/drn models
* train first with cce loss, then with smooth_topk loss